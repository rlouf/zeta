//! Runs one direct-capability provider behind the typed IPC session.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, TryRecvError};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

#[cfg(unix)]
use std::os::unix::process::CommandExt;

use serde_json::{Map, Value};
use zeta_agent::{
    AbortReason, AbortSignal, AgentError, CapabilityExecutor, CapabilityFuture,
    CapabilityInvocation,
};
use zeta_ipc::{
    Action, ErrorObject, Frame, FrameReader, FrameWriter, PeerIdentity, RequestId, ResolvedRequest,
    Role, RuntimeConfig, Session, ShutdownDirection,
};

/// Describes one provider process and its inherited execution environment.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProcessLaunch {
    /// Names the installed extension in runtime errors.
    pub extension_id: String,
    /// Contains the executable followed by its arguments.
    pub argv: Vec<String>,
    /// Selects the provider process working directory.
    pub working_directory: Option<PathBuf>,
    /// Adds or replaces environment variables inherited by the provider.
    pub environment: BTreeMap<String, String>,
}

/// Bounds provider initialization, invocation, and shutdown.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ProcessExecutorConfig {
    /// Bounds the provider's initialization exchange.
    pub handshake_timeout: Duration,
    /// Bounds one direct provider request.
    pub call_timeout: Duration,
    /// Bounds an orderly provider shutdown.
    pub shutdown_timeout: Duration,
}

impl Default for ProcessExecutorConfig {
    fn default() -> Self {
        Self {
            handshake_timeout: Duration::from_secs(5),
            call_timeout: Duration::from_secs(30),
            shutdown_timeout: Duration::from_secs(2),
        }
    }
}

/// Lazily supervises one direct-capability provider process.
pub struct ProcessExecutor {
    launch: ProcessLaunch,
    config: ProcessExecutorConfig,
    process: Option<ProcessState>,
    next_request_id: u64,
    initialization_count: u64,
}

impl ProcessExecutor {
    /// Creates an executor with the default lifecycle timeouts.
    ///
    /// # Errors
    ///
    /// Returns [`AgentError`] when the launch description is incomplete.
    pub fn new(launch: ProcessLaunch) -> Result<Self, AgentError> {
        Self::with_config(launch, ProcessExecutorConfig::default())
    }

    /// Creates an executor with explicit lifecycle timeouts.
    ///
    /// # Errors
    ///
    /// Returns [`AgentError`] when the launch description is incomplete or a
    /// timeout is zero.
    pub fn with_config(
        launch: ProcessLaunch,
        config: ProcessExecutorConfig,
    ) -> Result<Self, AgentError> {
        validate_launch(&launch, config)?;
        Ok(Self {
            launch,
            config,
            process: None,
            next_request_id: 1,
            initialization_count: 0,
        })
    }

    /// Returns the number of completed process initialization exchanges.
    pub fn initialization_count(&self) -> u64 {
        self.initialization_count
    }

    /// Returns whether this executor currently owns a provider process.
    pub fn is_running(&self) -> bool {
        self.process.is_some()
    }

    /// Requests orderly shutdown and always reaps the child process.
    ///
    /// # Errors
    ///
    /// Returns [`AgentError`] when the provider rejects shutdown or its IPC
    /// connection fails. The child is still closed and reaped.
    pub fn shutdown(&mut self) -> Result<(), AgentError> {
        let Some(mut process) = self.process.take() else {
            return Ok(());
        };
        let request_id = self.request_id("shutdown");
        let mut params = Map::new();
        params.insert(
            "reason".to_owned(),
            Value::String("runtime shutdown".to_owned()),
        );
        let outcome = transact(
            &mut process,
            request_id,
            "shutdown",
            params,
            self.config.shutdown_timeout,
            None,
        );
        if outcome.is_ok() {
            wait_for_exit(&mut process.child, self.config.shutdown_timeout);
        }
        terminate(process);
        match outcome {
            Ok(_) => Ok(()),
            Err(ProcessFailure::Remote(error)) => Err(provider_error(error)),
            Err(ProcessFailure::Timeout) => Err(AgentError::tool("provider shutdown timed out")),
            Err(ProcessFailure::Aborted(reason)) => Err(AgentError::tool(format!(
                "provider shutdown aborted: {reason}"
            ))),
            Err(ProcessFailure::MalformedResponse(message))
            | Err(ProcessFailure::Transport(message)) => Err(AgentError::tool(message)),
        }
    }

    /// Calls one declared direct provider method.
    ///
    /// # Errors
    ///
    /// Returns [`AgentError`] when the provider cannot start, does not declare
    /// the method, rejects the call, returns a non-object result, or times out.
    pub fn call(
        &mut self,
        method: &str,
        input: Map<String, Value>,
        base_directory: Option<String>,
        effect_key: Option<String>,
        abort: &dyn AbortSignal,
    ) -> Result<Map<String, Value>, AgentError> {
        self.call_now(method, input, base_directory, effect_key, abort)
    }

    fn execute_now(
        &mut self,
        invocation: &CapabilityInvocation,
        abort: &dyn AbortSignal,
    ) -> Result<Map<String, Value>, AgentError> {
        self.call_now(
            invocation.capability_id.as_str(),
            invocation.params.clone(),
            invocation.base_directory.clone(),
            invocation.effect_key.clone(),
            abort,
        )
    }

    fn call_now(
        &mut self,
        method: &str,
        input: Map<String, Value>,
        base_directory: Option<String>,
        effect_key: Option<String>,
        abort: &dyn AbortSignal,
    ) -> Result<Map<String, Value>, AgentError> {
        if let Some(reason) = abort.reason() {
            return Err(AgentError::tool(format!("provider call aborted: {reason}")));
        }
        self.ensure_process(abort)?;
        let Some(process) = self.process.as_ref() else {
            unreachable!("ensure_process established the provider process")
        };
        if !process.methods.contains(method) {
            return Err(AgentError::tool(format!(
                "provider '{}' did not declare method '{method}'",
                self.launch.extension_id
            )));
        }

        let request_id = self.request_id("call");
        let params = direct_request_params(input, base_directory, effect_key);
        let Some(process) = self.process.as_mut() else {
            unreachable!("the provider process remained available")
        };
        let outcome = transact(
            process,
            request_id,
            method,
            params,
            self.config.call_timeout,
            Some(abort),
        );
        if call_outcome_invalidates_process(&outcome) {
            self.terminate_current_provider();
        }
        match outcome {
            Ok(Value::Object(result)) => Ok(result),
            Ok(Value::Null) | Ok(Value::Bool(_)) | Ok(Value::Number(_)) | Ok(Value::String(_))
            | Ok(Value::Array(_)) => Err(AgentError::tool(format!(
                "provider call '{method}' returned a non-object result"
            ))),
            Err(ProcessFailure::Remote(error)) => Err(provider_error(error)),
            Err(ProcessFailure::Timeout) => Err(AgentError::tool(format!(
                "provider call '{method}' timed out"
            ))),
            Err(ProcessFailure::Aborted(reason)) => Err(AgentError::tool(format!(
                "provider call '{method}' aborted: {reason}"
            ))),
            Err(ProcessFailure::MalformedResponse(message))
            | Err(ProcessFailure::Transport(message)) => Err(AgentError::tool(message)),
        }
    }

    fn ensure_process(&mut self, abort: &dyn AbortSignal) -> Result<(), AgentError> {
        if self.process.is_some() {
            return Ok(());
        }
        let process =
            match spawn_and_initialize_provider(&self.launch, self.config.handshake_timeout, abort)
            {
                Ok(process) => process,
                Err(error) => return Err(AgentError::tool(error)),
            };
        self.process = Some(process);
        self.initialization_count = self.initialization_count.saturating_add(1);
        Ok(())
    }

    fn request_id(&mut self, purpose: &str) -> RequestId {
        let sequence = self.next_request_id;
        self.next_request_id = self.next_request_id.saturating_add(1);
        RequestId::from(format!("runtime-{purpose}-{sequence}"))
    }

    fn terminate_current_provider(&mut self) {
        let Some(process) = self.process.take() else {
            return;
        };
        terminate(process);
    }
}

impl fmt::Debug for ProcessExecutor {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ProcessExecutor")
            .field("launch", &self.launch)
            .field("config", &self.config)
            .field("is_running", &self.is_running())
            .field("next_request_id", &self.next_request_id)
            .field("initialization_count", &self.initialization_count)
            .finish()
    }
}

impl CapabilityExecutor for ProcessExecutor {
    fn execute<'a>(
        &'a mut self,
        invocation: &'a CapabilityInvocation,
        abort: &'a dyn AbortSignal,
    ) -> CapabilityFuture<'a> {
        Box::pin(async move { self.execute_now(invocation, abort) })
    }
}

impl Drop for ProcessExecutor {
    fn drop(&mut self) {
        let _result = self.shutdown();
    }
}

struct ProcessState {
    child: Child,
    writer: FrameWriter<ChildStdin>,
    events: Receiver<ReaderEvent>,
    reader: JoinHandle<()>,
    session: Session,
    methods: BTreeSet<String>,
}

enum ReaderEvent {
    Frame(Frame),
    Closed,
    Failed(String),
}

enum ProcessFailure {
    Remote(ErrorObject),
    Timeout,
    Aborted(AbortReason),
    MalformedResponse(String),
    Transport(String),
}

impl ProcessFailure {
    fn invalidates_process(&self) -> bool {
        match self {
            ProcessFailure::Remote(_) => false,
            ProcessFailure::Timeout
            | ProcessFailure::Aborted(_)
            | ProcessFailure::MalformedResponse(_)
            | ProcessFailure::Transport(_) => true,
        }
    }
}

fn call_outcome_invalidates_process(outcome: &Result<Value, ProcessFailure>) -> bool {
    match outcome {
        Ok(Value::Object(_)) => false,
        Ok(Value::Null) | Ok(Value::Bool(_)) | Ok(Value::Number(_)) | Ok(Value::String(_))
        | Ok(Value::Array(_)) => true,
        Err(failure) => failure.invalidates_process(),
    }
}

fn validate_launch(
    launch: &ProcessLaunch,
    config: ProcessExecutorConfig,
) -> Result<(), AgentError> {
    if launch.extension_id.trim().is_empty() {
        return Err(AgentError::tool(
            "process launch extension id must not be empty",
        ));
    }
    let Some(executable) = launch.argv.first() else {
        return Err(AgentError::tool("process launch argv must not be empty"));
    };
    if executable.trim().is_empty() {
        return Err(AgentError::tool(
            "process launch executable must not be empty",
        ));
    }
    if config.handshake_timeout.is_zero()
        || config.call_timeout.is_zero()
        || config.shutdown_timeout.is_zero()
    {
        return Err(AgentError::tool(
            "process timeouts must be greater than zero",
        ));
    }
    Ok(())
}

fn spawn_and_initialize_provider(
    launch: &ProcessLaunch,
    timeout: Duration,
    abort: &dyn AbortSignal,
) -> Result<ProcessState, String> {
    let executable = launch
        .argv
        .first()
        .expect("validated launch descriptions contain an executable");
    let mut command = Command::new(executable);
    command
        .args(&launch.argv[1..])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .envs(&launch.environment);
    #[cfg(unix)]
    command.process_group(0);
    if let Some(directory) = &launch.working_directory {
        command.current_dir(directory);
    }
    let child = command.spawn();
    let Ok(mut child) = child else {
        return Err(format!(
            "failed to start provider '{}': {}",
            launch.extension_id,
            child.expect_err("the let-else observed a spawn error")
        ));
    };
    let Some(stdin) = child.stdin.take() else {
        kill_and_wait(&mut child);
        return Err(format!(
            "provider '{}' has no writable stdin",
            launch.extension_id
        ));
    };
    let Some(stdout) = child.stdout.take() else {
        drop(stdin);
        kill_and_wait(&mut child);
        return Err(format!(
            "provider '{}' has no readable stdout",
            launch.extension_id
        ));
    };

    let (sender, events) = mpsc::channel();
    let reader = thread::Builder::new()
        .name("zeta-provider-stdout".to_owned())
        .spawn(move || {
            let mut reader = FrameReader::new(stdout);
            loop {
                match reader.read_frame() {
                    Ok(Some(frame)) => {
                        if sender.send(ReaderEvent::Frame(frame)).is_err() {
                            return;
                        }
                    }
                    Ok(None) => {
                        let _result = sender.send(ReaderEvent::Closed);
                        return;
                    }
                    Err(error) => {
                        let _result = sender.send(ReaderEvent::Failed(error.to_string()));
                        return;
                    }
                }
            }
        });
    let Ok(reader) = reader else {
        drop(stdin);
        kill_and_wait(&mut child);
        return Err(format!(
            "failed to start provider stdout reader: {}",
            reader.expect_err("the let-else observed a thread spawn error")
        ));
    };
    let runtime = PeerIdentity::new("zeta", env!("CARGO_PKG_VERSION"));
    let session = Session::runtime(
        RuntimeConfig::new(runtime),
        ShutdownDirection::LocalSupervisesRemote,
    );
    let mut process = ProcessState {
        child,
        writer: FrameWriter::new(stdin),
        events,
        reader,
        session,
        methods: BTreeSet::new(),
    };
    let initialized = initialize(&mut process, timeout, abort);
    if let Err(error) = initialized {
        terminate(process);
        return Err(error);
    }
    Ok(process)
}

fn initialize(
    process: &mut ProcessState,
    timeout: Duration,
    abort: &dyn AbortSignal,
) -> Result<(), String> {
    let deadline = Instant::now() + timeout;
    while !process.session.is_initialized() {
        let frame = receive_frame(process, deadline, Some(abort)).map_err(failure_message)?;
        let Frame::Message(message) = frame else {
            let Frame::Violation(violation) = frame else {
                unreachable!("Frame has exactly two variants")
            };
            return Err(format!(
                "provider sent invalid IPC frame ({}): {}",
                violation.rule, violation.detail
            ));
        };
        let actions = process.session.receive(message);
        drive_actions(process, actions)?;
    }
    let Some(parameters) = process.session.peer_parameters() else {
        return Err("provider initialization parameters are unavailable".to_owned());
    };
    if !parameters.roles.contains(&Role::Provider) {
        return Err("provider process did not request the provider role".to_owned());
    }
    let Some(methods) = &parameters.methods else {
        return Err("provider process did not declare direct methods".to_owned());
    };
    for method in methods {
        process.methods.insert(method.name.clone());
    }
    Ok(())
}

fn direct_request_params(
    input: Map<String, Value>,
    base_directory: Option<String>,
    effect_key: Option<String>,
) -> Map<String, Value> {
    let mut params = Map::new();
    params.insert("input".to_owned(), Value::Object(input));
    if let Some(base_directory) = base_directory {
        params.insert("base_dir".to_owned(), Value::String(base_directory));
    }
    if let Some(effect_key) = effect_key {
        params.insert("effect_key".to_owned(), Value::String(effect_key));
    }
    params
}

fn transact(
    process: &mut ProcessState,
    request_id: RequestId,
    method: &str,
    params: Map<String, Value>,
    timeout: Duration,
    abort: Option<&dyn AbortSignal>,
) -> Result<Value, ProcessFailure> {
    if let Some(reason) = abort.and_then(|signal| signal.reason()) {
        return Err(ProcessFailure::Aborted(reason));
    }
    let actions = process
        .session
        .send_request(request_id.clone(), method, params)
        .map_err(|error| ProcessFailure::Transport(error.to_string()))?;
    drive_actions(process, actions).map_err(ProcessFailure::Transport)?;
    let deadline = Instant::now() + timeout;
    loop {
        let frame = receive_frame(process, deadline, abort)?;
        let Frame::Message(message) = frame else {
            let Frame::Violation(violation) = frame else {
                unreachable!("Frame has exactly two variants")
            };
            return Err(ProcessFailure::Transport(format!(
                "provider sent invalid IPC frame ({}): {}",
                violation.rule, violation.detail
            )));
        };
        let actions = process.session.receive(message);
        let malformed_response = actions
            .iter()
            .any(|action| matches!(action, Action::Violation(_)));
        let resolved = drive_actions(process, actions).map_err(|message| {
            if malformed_response {
                ProcessFailure::MalformedResponse(message)
            } else {
                ProcessFailure::Transport(message)
            }
        })?;
        let Some(resolved) = resolved else {
            continue;
        };
        if resolved.id != request_id {
            return Err(ProcessFailure::Transport(format!(
                "provider resolved unexpected request '{}'",
                resolved.id
            )));
        }
        return resolved.outcome.map_err(ProcessFailure::Remote);
    }
}

fn receive_frame(
    process: &ProcessState,
    deadline: Instant,
    abort: Option<&dyn AbortSignal>,
) -> Result<Frame, ProcessFailure> {
    loop {
        match process.events.try_recv() {
            Ok(event) => return reader_event(event),
            Err(TryRecvError::Empty) => {}
            Err(TryRecvError::Disconnected) => {
                return Err(ProcessFailure::Transport(
                    "provider stdout reader stopped".to_owned(),
                ))
            }
        }
        if let Some(reason) = abort.and_then(|signal| signal.reason()) {
            return Err(ProcessFailure::Aborted(reason));
        }
        let Some(remaining) = deadline.checked_duration_since(Instant::now()) else {
            return Err(ProcessFailure::Timeout);
        };
        match process
            .events
            .recv_timeout(remaining.min(Duration::from_millis(10)))
        {
            Ok(event) => return reader_event(event),
            Err(RecvTimeoutError::Timeout) => continue,
            Err(RecvTimeoutError::Disconnected) => {
                return Err(ProcessFailure::Transport(
                    "provider stdout reader stopped".to_owned(),
                ))
            }
        }
    }
}

fn reader_event(event: ReaderEvent) -> Result<Frame, ProcessFailure> {
    match event {
        ReaderEvent::Frame(frame) => Ok(frame),
        ReaderEvent::Closed => Err(ProcessFailure::Transport(
            "provider closed stdout".to_owned(),
        )),
        ReaderEvent::Failed(message) => Err(ProcessFailure::Transport(format!(
            "provider stdout failed: {message}"
        ))),
    }
}

fn drive_actions(
    process: &mut ProcessState,
    actions: Vec<Action>,
) -> Result<Option<ResolvedRequest>, String> {
    let mut resolved = None;
    for action in actions {
        match action {
            Action::Send(message) => process
                .writer
                .write_message(&message)
                .map_err(|error| format!("provider stdin failed: {error}"))?,
            Action::RequestResolved(request) => {
                if resolved.is_some() {
                    return Err("IPC session resolved more than one request".to_owned());
                }
                resolved = Some(request);
            }
            Action::HandleRequest(request) => {
                return Err(format!(
                    "provider sent unsupported request '{}'",
                    request.method
                ));
            }
            Action::HandleNotification(notification) => {
                return Err(format!(
                    "provider sent unsupported notification '{}'",
                    notification.method
                ));
            }
            Action::Violation(error) => return Err(error.to_string()),
            Action::Close { reason } => {
                return Err(reason.unwrap_or_else(|| "IPC session closed".to_owned()));
            }
        }
    }
    Ok(resolved)
}

fn provider_error(error: ErrorObject) -> AgentError {
    let details = error.data.as_ref().and_then(Value::as_object);
    let stable_code = details
        .and_then(|details| details.get("code"))
        .and_then(Value::as_str)
        .unwrap_or("provider_error");
    let retryable = details
        .and_then(|details| details.get("retryable"))
        .and_then(Value::as_bool)
        .unwrap_or(false);
    AgentError::tool(format!(
        "provider call failed [{stable_code}, retryable={retryable}]: {}",
        error.message
    ))
}

fn failure_message(error: ProcessFailure) -> String {
    match error {
        ProcessFailure::Remote(error) => provider_error(error).message,
        ProcessFailure::Timeout => "provider initialization timed out".to_owned(),
        ProcessFailure::Aborted(reason) => {
            format!("provider initialization aborted: {reason}")
        }
        ProcessFailure::MalformedResponse(message) | ProcessFailure::Transport(message) => message,
    }
}

fn wait_for_exit(child: &mut Child, timeout: Duration) {
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(_status)) => return,
            Ok(None) => {}
            Err(_error) => return,
        }
        let Some(remaining) = deadline.checked_duration_since(Instant::now()) else {
            return;
        };
        thread::sleep(remaining.min(Duration::from_millis(10)));
    }
}

fn terminate(process: ProcessState) {
    let ProcessState {
        mut child,
        writer,
        events,
        reader,
        session: _,
        methods: _,
    } = process;
    drop(writer);
    drop(events);
    kill_and_wait(&mut child);
    let _result = reader.join();
}

fn kill_and_wait(child: &mut Child) {
    #[cfg(unix)]
    let group_killed = kill_process_group(child).is_ok();
    #[cfg(not(unix))]
    let group_killed = false;
    match child.try_wait() {
        Ok(Some(_status)) => return,
        Ok(None) if !group_killed => {
            let _result = child.kill();
        }
        Ok(None) => {}
        Err(_error) => {
            if !group_killed {
                let _result = child.kill();
            }
        }
    }
    let _result = child.wait();
}

#[cfg(unix)]
fn kill_process_group(child: &Child) -> Result<(), std::io::Error> {
    const SIGKILL: i32 = 9;

    unsafe extern "C" {
        fn kill(process: i32, signal: i32) -> i32;
    }

    let process_group = i32::try_from(child.id())
        .map_err(|_error| std::io::Error::other("provider process id exceeds i32"))?;
    // SAFETY: the child was placed in a process group whose id is its pid.
    let result = unsafe { kill(-process_group, SIGKILL) };
    if result == 0 {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error())
    }
}
