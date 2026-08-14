//! Composes operating-system services for the native Zeta application.

use std::fmt;
use std::fs;
use std::io;
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde_json::{Map, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::unix::{OwnedReadHalf, OwnedWriteHalf};
use tokio::net::{UnixListener, UnixStream};
use tokio::sync::{broadcast, watch};
use tokio::task::{JoinHandle, JoinSet};
use zeta_agent::{AgentProposal, AgentRunResult, RunStopReason};
use zeta_dispatch::{AttemptCompletion, AttemptCompletionDisposition, AttemptControl};
use zeta_ipc::{
    validate_message, Action, ErrorObject, ErrorResponse, Frame, FrameReader, Message,
    Notification, PeerIdentity, Request, Role, RuntimeConfig, Session, ShutdownDirection,
    MAX_FRAME_BYTES, METHOD_NOT_FOUND,
};
use zeta_journal::Event;

mod host_model;
pub mod process_executor;
pub mod project_revision;
pub mod runtime;
pub mod runtime_services;

pub use process_executor::{ProcessExecutor, ProcessExecutorConfig, ProcessLaunch};
pub use project_revision::{
    ActiveAgent, ActiveProjectStatus, Project, ProjectError, ProjectRevision, ProjectRevisionStore,
};
pub use runtime::{IngressResult, Runtime, RuntimeError, RuntimeStatus, RuntimeWake};
pub use runtime_services::{
    prepare_agent, CallbackDraftRecorder, CallbackObserver, CancellationToken, ExecutorSelection,
    InvocationInputs, PrepareAgentError, PrepareAgentErrorKind, PreparedAgent, ScheduleStatus,
    Scheduler, SchedulerError, SchedulerErrorKind, SystemClock, UuidIdSource,
};

const LOCAL_NOTIFICATION_CAPACITY: usize = 64;

type LocalRequestHandler = dyn Fn(Request) -> Result<Value, ErrorObject> + Send + Sync + 'static;

/// Selects the protocol surface and shutdown authority of a local socket.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LocalSocketPurpose {
    /// Serves application requests and event notifications without host shutdown authority.
    Application,
    /// Serves lifecycle requests and allows a client to request host shutdown.
    Control,
}

/// Configures one local socket with its purpose and shared runtime identity.
///
/// # Examples
///
/// ```
/// let application = zeta::LocalSocketConfig::application("instance-1");
/// let control = zeta::LocalSocketConfig::control("instance-1");
/// # let _configs = (application, control);
/// ```
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LocalSocketConfig {
    purpose: LocalSocketPurpose,
    instance_id: String,
}

impl LocalSocketConfig {
    /// Configures an application endpoint with shutdown disabled.
    pub fn application(instance_id: impl Into<String>) -> Self {
        Self {
            purpose: LocalSocketPurpose::Application,
            instance_id: instance_id.into(),
        }
    }

    /// Configures a client-only lifecycle endpoint with shutdown enabled.
    pub fn control(instance_id: impl Into<String>) -> Self {
        Self {
            purpose: LocalSocketPurpose::Control,
            instance_id: instance_id.into(),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct SocketIdentity {
    device: u64,
    inode: u64,
}

/// Reports a local client socket setup or lifecycle failure.
///
/// # Examples
///
/// ```
/// # async fn example() {
/// let error = zeta::LocalSocketServer::bind(
///     "relative.sock",
///     zeta::LocalSocketConfig::application("instance-1"),
///     |_request| Ok::<_, zeta_ipc::ErrorObject>(serde_json::json!({})),
/// )
/// .await
/// .unwrap_err();
/// assert_eq!(error.reason(), "relative_path");
/// # }
/// ```
#[derive(Debug)]
pub struct LocalSocketError {
    reason: &'static str,
    path: PathBuf,
    detail: String,
}

impl LocalSocketError {
    fn new(reason: &'static str, path: &Path, detail: impl Into<String>) -> Self {
        Self {
            reason,
            path: path.to_path_buf(),
            detail: detail.into(),
        }
    }

    /// Returns the stable machine-readable failure class.
    ///
    /// # Examples
    ///
    /// ```
    /// # async fn example() {
    /// let error = zeta::LocalSocketServer::bind(
    ///     "relative.sock",
    ///     zeta::LocalSocketConfig::application("instance-1"),
    ///     |_request| Ok::<_, zeta_ipc::ErrorObject>(serde_json::json!({})),
    /// )
    /// .await
    /// .unwrap_err();
    /// assert_eq!(error.reason(), "relative_path");
    /// # }
    /// ```
    pub fn reason(&self) -> &'static str {
        self.reason
    }
}

impl fmt::Display for LocalSocketError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let Self {
            reason,
            path,
            detail,
        } = self;
        write!(formatter, "{reason} at '{}': {detail}", path.display())
    }
}

impl std::error::Error for LocalSocketError {}

/// Owns one project-local Unix socket for native client connections.
///
/// The socket accepts the existing `zeta-ipc` client protocol while process
/// ownership remains with the caller. Application endpoints keep `shutdown`
/// disabled; control endpoints report accepted shutdown requests to the host.
///
/// # Examples
///
/// ```no_run
/// # async fn example() -> Result<(), Box<dyn std::error::Error>> {
/// let path = std::env::temp_dir().join("zeta-example.sock");
/// let server = zeta::LocalSocketServer::bind(
///     path,
///     zeta::LocalSocketConfig::application("instance-1"),
///     |_request| Ok::<_, zeta_ipc::ErrorObject>(serde_json::json!({})),
/// )
/// .await?;
/// server.shutdown().await?;
/// # Ok(())
/// # }
/// ```
pub struct LocalSocketServer {
    path: PathBuf,
    notifications: broadcast::Sender<Map<String, Value>>,
    host_shutdown: watch::Sender<bool>,
    shutdown: watch::Sender<bool>,
    task: Option<JoinHandle<Result<(), LocalSocketError>>>,
}

impl LocalSocketServer {
    /// Binds an explicit absolute socket path and starts accepting clients.
    ///
    /// Existing files, symlinks, live sockets, and stale sockets are refused.
    /// The caller must create the parent directory before binding.
    ///
    /// # Examples
    ///
    /// ```no_run
    /// # async fn example() -> Result<(), Box<dyn std::error::Error>> {
    /// let path = std::env::temp_dir().join("zeta-bind-example.sock");
    /// let server = zeta::LocalSocketServer::bind(
    ///     path,
    ///     zeta::LocalSocketConfig::application("instance-1"),
    ///     |_request| Ok::<_, zeta_ipc::ErrorObject>(serde_json::json!({})),
    /// )
    /// .await?;
    /// server.shutdown().await?;
    /// # Ok(())
    /// # }
    /// ```
    ///
    /// # Errors
    ///
    /// Returns [`LocalSocketError`] when the path is relative or occupied, the
    /// listener cannot bind, or owner-only permissions cannot be applied.
    pub async fn bind<H>(
        path: impl AsRef<Path>,
        config: LocalSocketConfig,
        handler: H,
    ) -> Result<Self, LocalSocketError>
    where
        H: Fn(Request) -> Result<Value, ErrorObject> + Send + Sync + 'static,
    {
        let path = path.as_ref().to_path_buf();
        if !path.is_absolute() {
            return Err(LocalSocketError::new(
                "relative_path",
                &path,
                "the local socket path must be absolute",
            ));
        }
        match fs::symlink_metadata(&path) {
            Ok(_metadata) => {
                return Err(LocalSocketError::new(
                    "path_occupied",
                    &path,
                    "the local socket path already exists",
                ));
            }
            Err(error) => {
                if error.kind() != io::ErrorKind::NotFound {
                    return Err(LocalSocketError::new("inspect", &path, error.to_string()));
                }
            }
        }

        let listener = UnixListener::bind(&path);
        let Ok(listener) = listener else {
            let error = listener.expect_err("the let-else observed a bind error");
            return Err(LocalSocketError::new("bind", &path, error.to_string()));
        };
        let identity = socket_identity(&path);
        let Ok(identity) = identity else {
            let error = identity.expect_err("the let-else observed an identity error");
            return Err(LocalSocketError::new("inspect", &path, error.to_string()));
        };
        let permissions = fs::Permissions::from_mode(0o600);
        let permission_result = fs::set_permissions(&path, permissions);
        let Ok(()) = permission_result else {
            let error = permission_result.expect_err("the let-else observed a permissions error");
            drop(listener);
            let _cleanup = remove_owned_socket(&path, identity);
            return Err(LocalSocketError::new(
                "permissions",
                &path,
                error.to_string(),
            ));
        };

        let handler: Arc<LocalRequestHandler> = Arc::new(handler);
        let (notifications, _notification_receiver) =
            broadcast::channel(LOCAL_NOTIFICATION_CAPACITY);
        let (host_shutdown, _host_shutdown_receiver) = watch::channel(false);
        let (shutdown, shutdown_receiver) = watch::channel(false);
        let task_path = path.clone();
        let task_notifications = notifications.clone();
        let task_host_shutdown = host_shutdown.clone();
        let task = tokio::spawn(async move {
            let result = run_local_listener(
                listener,
                &task_path,
                config,
                handler,
                task_notifications,
                task_host_shutdown,
                shutdown_receiver,
            )
            .await;
            let cleanup = remove_owned_socket(&task_path, identity);
            match (result, cleanup) {
                (Ok(()), Ok(())) => Ok(()),
                (Err(error), Ok(())) => Err(error),
                (Ok(()), Err(error)) => Err(error),
                (Err(error), Err(_cleanup_error)) => Err(error),
            }
        });
        Ok(Self {
            path,
            notifications,
            host_shutdown,
            shutdown,
            task: Some(task),
        })
    }

    /// Subscribes to accepted control requests that should stop the host.
    ///
    /// The value becomes `true` only after the successful control response is
    /// flushed and the connection's write side is closed.
    pub fn host_shutdown(&self) -> watch::Receiver<bool> {
        self.host_shutdown.subscribe()
    }

    /// Reports whether the listener task stopped before explicit shutdown.
    pub fn is_finished(&self) -> bool {
        self.task.as_ref().is_none_or(JoinHandle::is_finished)
    }

    /// Broadcasts one committed event to every initialized client.
    ///
    /// Clients recover missed or lagged notifications through `events.list`.
    /// Sending while no client is connected succeeds with a receiver count of
    /// zero.
    ///
    /// # Examples
    ///
    /// ```no_run
    /// # async fn example(server: &zeta::LocalSocketServer, event: &zeta_journal::Event) -> Result<(), Box<dyn std::error::Error>> {
    /// let _receivers = server.notify_event(event)?;
    /// # Ok(())
    /// # }
    /// ```
    ///
    /// # Errors
    ///
    /// Returns [`LocalSocketError`] when the event cannot be serialized as a
    /// valid `zeta-ipc` event notification.
    pub fn notify_event(&self, event: &Event) -> Result<usize, LocalSocketError> {
        let event = serde_json::to_value(event);
        let Ok(event) = event else {
            let error = event.expect_err("the let-else observed a serialization error");
            return Err(LocalSocketError::new(
                "notification",
                &self.path,
                error.to_string(),
            ));
        };
        let mut params = Map::new();
        params.insert("event".to_owned(), event);
        let message = Message::Notification(Notification::new("event", params.clone()));
        let validation = validate_message(&message);
        let Ok(()) = validation else {
            let error = validation.expect_err("the let-else observed an invalid notification");
            return Err(LocalSocketError::new(
                "notification",
                &self.path,
                error.to_string(),
            ));
        };
        match self.notifications.send(params) {
            Ok(receivers) => Ok(receivers),
            Err(_error) => Ok(0),
        }
    }

    /// Stops the listener and removes only the socket entry that it bound.
    ///
    /// # Examples
    ///
    /// ```no_run
    /// # async fn example(server: zeta::LocalSocketServer) -> Result<(), zeta::LocalSocketError> {
    /// server.shutdown().await?;
    /// # Ok(())
    /// # }
    /// ```
    ///
    /// # Errors
    ///
    /// Returns [`LocalSocketError`] when the accept task fails or the owned
    /// socket entry cannot be removed.
    pub async fn shutdown(mut self) -> Result<(), LocalSocketError> {
        let _result = self.shutdown.send(true);
        let Some(task) = self.task.take() else {
            return Ok(());
        };
        match task.await {
            Ok(result) => result,
            Err(error) => Err(LocalSocketError::new("task", &self.path, error.to_string())),
        }
    }
}

impl fmt::Debug for LocalSocketServer {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("LocalSocketServer")
            .field("path", &self.path)
            .finish_non_exhaustive()
    }
}

impl Drop for LocalSocketServer {
    fn drop(&mut self) {
        let _result = self.shutdown.send(true);
    }
}

async fn run_local_listener(
    listener: UnixListener,
    path: &Path,
    config: LocalSocketConfig,
    handler: Arc<LocalRequestHandler>,
    notifications: broadcast::Sender<Map<String, Value>>,
    host_shutdown: watch::Sender<bool>,
    mut shutdown: watch::Receiver<bool>,
) -> Result<(), LocalSocketError> {
    let mut connections = JoinSet::new();
    loop {
        tokio::select! {
            changed = shutdown.changed() => {
                match changed {
                    Ok(()) => break,
                    Err(_closed) => break,
                }
            }
            accepted = listener.accept() => {
                let accepted = match accepted {
                    Ok(accepted) => accepted,
                    Err(error) => {
                        return Err(LocalSocketError::new("accept", path, error.to_string()));
                    }
                };
                let (stream, _address) = accepted;
                let config = config.clone();
                let handler = Arc::clone(&handler);
                let notifications = notifications.subscribe();
                let host_shutdown = host_shutdown.clone();
                let shutdown = shutdown.clone();
                connections.spawn(async move {
                    let _result = serve_local_connection(
                        stream,
                        config,
                        handler,
                        notifications,
                        host_shutdown,
                        shutdown,
                    )
                    .await;
                });
            }
            joined = connections.join_next(), if !connections.is_empty() => {
                match joined {
                    Some(Ok(())) => {}
                    Some(Err(_join_error)) => {}
                    None => {}
                }
            }
        }
    }
    connections.abort_all();
    while let Some(joined) = connections.join_next().await {
        match joined {
            Ok(()) => {}
            Err(_join_error) => {}
        }
    }
    Ok(())
}

async fn serve_local_connection(
    stream: UnixStream,
    socket_config: LocalSocketConfig,
    handler: Arc<LocalRequestHandler>,
    mut notifications: broadcast::Receiver<Map<String, Value>>,
    host_shutdown: watch::Sender<bool>,
    mut shutdown: watch::Receiver<bool>,
) -> Result<(), String> {
    let LocalSocketConfig {
        purpose,
        instance_id,
    } = socket_config;
    let (reader, mut writer) = stream.into_split();
    let mut reader = BufReader::new(reader);
    let runtime = PeerIdentity::new("zeta", env!("CARGO_PKG_VERSION"));
    let mut config = RuntimeConfig::new(runtime);
    config
        .config
        .insert("instance_id".to_owned(), Value::String(instance_id));
    let shutdown_direction = match purpose {
        LocalSocketPurpose::Application => {
            config.supported_roles = vec![Role::Client, Role::Source];
            ShutdownDirection::Disabled
        }
        LocalSocketPurpose::Control => {
            config.supported_roles = vec![Role::Client];
            ShutdownDirection::RemoteSupervisesLocal
        }
    };
    let mut session = Session::runtime(config, shutdown_direction);
    loop {
        tokio::select! {
            changed = shutdown.changed() => {
                match changed {
                    Ok(()) => return Ok(()),
                    Err(_closed) => return Ok(()),
                }
            }
            frame = read_local_frame(&mut reader) => {
                let frame = frame.map_err(|error| error.to_string())?;
                let Some(frame) = frame else {
                    return Ok(());
                };
                match frame {
                    Frame::Message(message) => {
                        let requests_host_shutdown = match &message {
                            Message::Request(request) => {
                                session.is_initialized() && request.method == "shutdown"
                            }
                            Message::Notification(_notification) => false,
                            Message::Success(_response) => false,
                            Message::Error(_response) => false,
                        };
                        let actions = session.receive(message);
                        if drive_local_actions(
                            &mut session,
                            &mut writer,
                            purpose,
                            &handler,
                            actions,
                        )
                        .await?
                        {
                            match purpose {
                                LocalSocketPurpose::Application => {}
                                LocalSocketPurpose::Control => {
                                    if requests_host_shutdown {
                                        writer
                                            .shutdown()
                                            .await
                                            .map_err(|error| error.to_string())?;
                                        drop(writer);
                                        drop(reader);
                                        host_shutdown.send_replace(true);
                                        return Ok(());
                                    }
                                }
                            }
                            return Ok(());
                        }
                    }
                    Frame::Violation(violation) => {
                        let error = ErrorObject::protocol(
                            violation.code,
                            violation.rule,
                            violation.detail,
                        );
                        let message = Message::Error(ErrorResponse::new(
                            violation.request_id,
                            error,
                        ));
                        write_local_message(&mut writer, &message).await?;
                    }
                }
            }
            notification = notifications.recv() => {
                match purpose {
                    LocalSocketPurpose::Application => {}
                    LocalSocketPurpose::Control => continue,
                }
                match notification {
                    Ok(params) => {
                        if !session.is_initialized() {
                            continue;
                        }
                        let actions = session.send_notification("event", params);
                        let Ok(actions) = actions else {
                            continue;
                        };
                        if drive_local_actions(
                            &mut session,
                            &mut writer,
                            purpose,
                            &handler,
                            actions,
                        )
                        .await?
                        {
                            return Ok(());
                        }
                    }
                    Err(broadcast::error::RecvError::Lagged(_count)) => continue,
                    Err(broadcast::error::RecvError::Closed) => return Ok(()),
                }
            }
        }
    }
}

async fn drive_local_actions(
    session: &mut Session,
    writer: &mut OwnedWriteHalf,
    purpose: LocalSocketPurpose,
    handler: &Arc<LocalRequestHandler>,
    actions: Vec<Action>,
) -> Result<bool, String> {
    let mut actions = std::collections::VecDeque::from(actions);
    while let Some(action) = actions.pop_front() {
        match action {
            Action::Send(message) => write_local_message(writer, &message).await?,
            Action::HandleRequest(request) => {
                let request_id = request.id.clone();
                let completed = match purpose {
                    LocalSocketPurpose::Application => {
                        let handler = Arc::clone(handler);
                        let outcome = tokio::task::spawn_blocking(move || handler(request)).await;
                        let outcome = match outcome {
                            Ok(outcome) => outcome,
                            Err(error) => return Err(error.to_string()),
                        };
                        match outcome {
                            Ok(result) => session.complete_request(&request_id, result),
                            Err(error) => session.fail_request(&request_id, error),
                        }
                    }
                    LocalSocketPurpose::Control => session.fail_request(
                        &request_id,
                        ErrorObject::new(METHOD_NOT_FOUND, "Method not found", None),
                    ),
                };
                let completed = match completed {
                    Ok(completed) => completed,
                    Err(error) => session
                        .fail_request(&request_id, ErrorObject::from(error))
                        .map_err(|error| error.to_string())?,
                };
                for action in completed {
                    actions.push_back(action);
                }
            }
            Action::HandleNotification(notification) => {
                return Err(format!(
                    "the local runtime cannot handle notification {:?}",
                    notification.method
                ));
            }
            Action::RequestResolved(request) => {
                return Err(format!(
                    "the local runtime resolved unexpected request {:?}",
                    request.id
                ));
            }
            Action::Violation(error) => return Err(error.to_string()),
            Action::Close { reason: _reason } => return Ok(true),
        }
    }
    Ok(false)
}

async fn write_local_message(writer: &mut OwnedWriteHalf, message: &Message) -> Result<(), String> {
    let mut line = message.to_json().into_bytes();
    line.push(b'\n');
    writer
        .write_all(&line)
        .await
        .map_err(|error| error.to_string())?;
    writer.flush().await.map_err(|error| error.to_string())
}

async fn read_local_frame(reader: &mut BufReader<OwnedReadHalf>) -> io::Result<Option<Frame>> {
    let mut line = Vec::new();
    let mut overlong = false;
    loop {
        let buffer = reader.fill_buf().await?;
        if buffer.is_empty() {
            if line.is_empty() && !overlong {
                return Ok(None);
            }
            return decode_local_frame(line, false);
        }
        let mut newline = None;
        for (index, byte) in buffer.iter().enumerate() {
            if *byte == b'\n' {
                newline = Some(index);
                break;
            }
        }
        let (consumed, data_length, ended) = match newline {
            Some(index) => (index + 1, index, true),
            None => (buffer.len(), buffer.len(), false),
        };
        if !overlong {
            let maximum = MAX_FRAME_BYTES.saturating_add(1);
            let remaining = maximum.saturating_sub(line.len());
            let copy_length = data_length.min(remaining);
            line.extend_from_slice(&buffer[..copy_length]);
            if data_length > copy_length || line.len() > MAX_FRAME_BYTES {
                overlong = true;
            }
        }
        reader.consume(consumed);
        if ended {
            return decode_local_frame(line, true);
        }
    }
}

fn decode_local_frame(mut line: Vec<u8>, terminated: bool) -> io::Result<Option<Frame>> {
    if terminated {
        line.push(b'\n');
    }
    let mut reader = FrameReader::with_max_frame_bytes(line.as_slice(), MAX_FRAME_BYTES);
    reader.read_frame()
}

fn socket_identity(path: &Path) -> io::Result<SocketIdentity> {
    let metadata = fs::symlink_metadata(path)?;
    Ok(SocketIdentity {
        device: metadata.dev(),
        inode: metadata.ino(),
    })
}

fn remove_owned_socket(path: &Path, identity: SocketIdentity) -> Result<(), LocalSocketError> {
    let metadata = fs::symlink_metadata(path);
    let metadata = match metadata {
        Ok(metadata) => metadata,
        Err(error) => {
            if error.kind() == io::ErrorKind::NotFound {
                return Ok(());
            }
            return Err(LocalSocketError::new("cleanup", path, error.to_string()));
        }
    };
    let current = SocketIdentity {
        device: metadata.dev(),
        inode: metadata.ino(),
    };
    if !metadata.file_type().is_socket() || current != identity {
        return Ok(());
    }
    fs::remove_file(path).map_err(|error| LocalSocketError::new("cleanup", path, error.to_string()))
}

/// Classifies a failure while handing an agent result to Dispatch.
///
/// # Examples
///
/// ```
/// let kind = zeta::CompletionHandoffErrorKind::MalformedUsage;
/// assert_eq!(kind.reason(), "malformed_usage");
/// ```
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CompletionHandoffErrorKind {
    /// A platform-sized request position does not fit Dispatch's durable type.
    ControlPositionOverflow,
    /// Agent telemetry carries a non-object usage value.
    MalformedUsage,
}

impl CompletionHandoffErrorKind {
    /// Returns a stable machine-readable reason.
    pub fn reason(self) -> &'static str {
        match self {
            CompletionHandoffErrorKind::ControlPositionOverflow => "control_position_overflow",
            CompletionHandoffErrorKind::MalformedUsage => "malformed_usage",
        }
    }
}

/// Reports why an agent result cannot become a Dispatch completion.
///
/// # Examples
///
/// ```
/// # fn inspect(error: &zeta::CompletionHandoffError) {
/// assert!(!error.detail().is_empty());
/// let _kind = error.kind();
/// # }
/// ```
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CompletionHandoffError {
    kind: CompletionHandoffErrorKind,
    detail: String,
}

impl CompletionHandoffError {
    fn new(kind: CompletionHandoffErrorKind, detail: impl Into<String>) -> Self {
        CompletionHandoffError {
            kind,
            detail: detail.into(),
        }
    }

    /// Returns the stable failure class.
    pub fn kind(&self) -> CompletionHandoffErrorKind {
        self.kind
    }

    /// Returns the stable machine-readable reason.
    pub fn reason(&self) -> &'static str {
        self.kind.reason()
    }

    /// Returns the human-readable failure detail.
    pub fn detail(&self) -> &str {
        &self.detail
    }
}

impl fmt::Display for CompletionHandoffError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.reason(), self.detail)
    }
}

impl std::error::Error for CompletionHandoffError {}

/// Converts one successful agent result into a typed Dispatch completion.
///
/// # Examples
///
/// ```
/// let result = zeta_agent::AgentRunResult {
///     final_answer: "done".to_owned(),
///     ..zeta_agent::AgentRunResult::default()
/// };
/// let completion = zeta::attempt_completion("2026-08-12T10:00:01Z", &result)?;
/// assert_eq!(completion.metadata()["final_answer"], "done");
/// # Ok::<(), zeta::CompletionHandoffError>(())
/// ```
///
/// # Errors
///
/// Returns [`CompletionHandoffError`] when usage telemetry is not an object, a
/// control position does not fit Dispatch's durable `u64`, or the result
/// contains a content-promotion proposal that Dispatch cannot commit atomically.
pub fn attempt_completion(
    finished_at: impl Into<String>,
    result: &AgentRunResult,
) -> Result<AttemptCompletion, CompletionHandoffError> {
    let mut metadata = Map::new();
    metadata.insert(
        "final_answer".to_owned(),
        Value::String(result.final_answer.clone()),
    );
    if let Some(final_object_id) = &result.final_object_id {
        metadata.insert(
            "final_object_id".to_owned(),
            Value::String(final_object_id.clone()),
        );
    }
    if let Some(stop_reason) = result.stop_reason {
        let stop_reason = match stop_reason {
            RunStopReason::Finished => "finished",
            RunStopReason::ToolStop => "tool_stop",
            RunStopReason::MaxModelCalls => "max_model_calls",
        };
        metadata.insert(
            "stop_reason".to_owned(),
            Value::String(stop_reason.to_owned()),
        );
    }
    if !result.events.is_empty() {
        let mut events = Vec::new();
        for event in &result.events {
            events.push(draft_event_value(event));
        }
        metadata.insert("events".to_owned(), Value::Array(events));
    }
    if let Some(usage) = result.telemetry.get("usage") {
        let Value::Object(usage) = usage else {
            return Err(CompletionHandoffError::new(
                CompletionHandoffErrorKind::MalformedUsage,
                "telemetry.usage must be a JSON object",
            ));
        };
        metadata.insert("usage".to_owned(), Value::Object(usage.clone()));
    }

    let mut controls = Vec::new();
    for proposal in &result.proposals {
        match proposal {
            AgentProposal::Publish {
                handle,
                event_type,
                payload,
                at,
                position,
            } => {
                let position = completion_position(*position)?;
                controls.push(AttemptControl::publish(
                    handle,
                    event_type,
                    payload.clone(),
                    at.clone(),
                    position,
                ));
            }
            AgentProposal::Wait {
                handle,
                event_type,
                fields,
                deadline,
                position,
            } => {
                let position = completion_position(*position)?;
                controls.push(AttemptControl::wait(
                    handle,
                    event_type,
                    fields.clone(),
                    deadline.clone(),
                    position,
                ));
            }
            AgentProposal::Cancel {
                handle,
                reason,
                source_agent_id,
                source_session_id,
                position,
            } => {
                let position = completion_position(*position)?;
                controls.push(AttemptControl::cancel(
                    handle,
                    reason.clone(),
                    source_agent_id,
                    source_session_id,
                    position,
                ));
            }
        }
    }

    Ok(AttemptCompletion::new(
        finished_at,
        AttemptCompletionDisposition::Succeeded,
        metadata,
        controls,
    ))
}

fn completion_position(position: usize) -> Result<u64, CompletionHandoffError> {
    u64::try_from(position).map_err(|_error| {
        CompletionHandoffError::new(
            CompletionHandoffErrorKind::ControlPositionOverflow,
            format!("control position {position} does not fit u64"),
        )
    })
}

fn draft_event_value(event: &zeta_journal::DraftEvent) -> Value {
    let mut value = Map::new();
    value.insert("type".to_owned(), Value::String(event.event_type.clone()));
    value.insert("source".to_owned(), Value::String(event.source.clone()));
    value.insert("payload".to_owned(), Value::Object(event.payload.clone()));
    value.insert(
        "idempotency_key".to_owned(),
        optional_string_value(event.idempotency_key.as_ref()),
    );
    value.insert(
        "caused_by".to_owned(),
        optional_string_value(event.caused_by.as_ref()),
    );
    value.insert(
        "session_id".to_owned(),
        optional_string_value(event.session_id.as_ref()),
    );
    value.insert(
        "run_id".to_owned(),
        optional_string_value(event.run_id.as_ref()),
    );
    value.insert(
        "turn_id".to_owned(),
        optional_string_value(event.turn_id.as_ref()),
    );
    Value::Object(value)
}

fn optional_string_value(value: Option<&String>) -> Value {
    match value {
        Some(value) => Value::String(value.clone()),
        None => Value::Null,
    }
}

/// Converts a verified authored project into deterministic runtime routes.
///
/// Disabled agents are omitted. The manifest's slug order becomes the stable
/// route order, and authored accepted event types retain exact-match semantics.
///
/// # Examples
///
/// ```
/// # fn convert(
/// #     manifest: &zeta_manifest::ProjectManifest,
/// # ) -> Result<(), zeta_manifest::ManifestError> {
/// let routes = zeta::routes_from_project(manifest)?;
/// assert!(routes.iter().all(|route| !route.agent_id().is_empty()));
/// # Ok(())
/// # }
/// ```
///
/// # Errors
///
/// Returns [`zeta_manifest::ManifestError`] when the manifest body does not
/// match its canonical project revision or violates its schema contract.
pub fn routes_from_project(
    manifest: &zeta_manifest::ProjectManifest,
) -> Result<Vec<zeta_dispatch::Route>, zeta_manifest::ManifestError> {
    zeta_manifest::verify_project_manifest(manifest)?;
    let mut routes = Vec::new();
    for spec in manifest.agents.values() {
        if !spec.enabled {
            continue;
        }
        let mut accepts = Vec::new();
        for event_type in &spec.accepts {
            accepts.push(zeta_dispatch::EventPattern::exact(event_type));
        }
        let session = if spec.session == "shared" {
            zeta_dispatch::SessionRule::Shared
        } else if spec.session == "per-event" {
            zeta_dispatch::SessionRule::PerEvent
        } else {
            zeta_dispatch::SessionRule::Template(spec.session.clone())
        };
        routes.push(zeta_dispatch::Route::new(
            &spec.slug,
            accepts,
            session,
            spec.locks.clone(),
            Some(manifest.id.to_string()),
        ));
    }
    Ok(routes)
}
