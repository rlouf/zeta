mod app;
mod wire;

use std::collections::HashMap;
use std::env;
use std::error::Error;
use std::ffi::{OsStr, OsString};
use std::io;
use std::process::Stdio;
#[cfg(unix)]
use std::sync::Arc;
#[cfg(unix)]
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use crossterm::event::{Event as TerminalEvent, EventStream};
use futures_util::StreamExt;
use serde_json::{Value, json};
use tokio::io::AsyncWriteExt;
use tokio::process::{ChildStdin, ChildStdout, Command};
use tokio::time::{Instant, MissedTickBehavior};
use tokio_util::codec::{FramedRead, LinesCodec, LinesCodecError};
use uuid::Uuid;
use zeta_ipc::{
    Action, ErrorObject, InitializeParams, InitializeResult, MAX_FRAME_BYTES, Message,
    PeerIdentity, RequestId, ResolvedRequest, Role, Session as IpcSession, ShutdownDirection,
};

use crate::app::{App, AppAction, TerminalSession};
use crate::wire::{Event, EventNotification, EventsListResult, SessionsListResult, SubmitResult};

const IPC_COMMAND_ARGS: [&str; 2] = ["ipc", "stdio"];
const REFRESH_INTERVAL: Duration = Duration::from_millis(500);
const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(10);
const LIFECYCLE_INTERVAL: Duration = Duration::from_millis(50);

type BoxError = Box<dyn Error>;

#[derive(Clone, Debug, PartialEq, Eq)]
enum RequestPurpose {
    RefreshSessions,
    RefreshEvents,
    Submit(String),
}

struct IpcTransport {
    child: tokio::process::Child,
    input: ChildStdin,
    output: FramedRead<ChildStdout, LinesCodec>,
    session: IpcSession,
}

struct Bootstrap {
    initialized: InitializeResult,
    sessions: SessionsListResult,
    events: EventsListResult,
    notifications: Vec<Event>,
}

#[derive(Debug, Default, PartialEq, Eq)]
struct ReconnectBackoff {
    failures: usize,
}

enum LoopEvent {
    Terminal(Option<Result<TerminalEvent, io::Error>>),
    Ipc(Option<Result<String, LinesCodecError>>),
    Refresh,
    Heartbeat,
    Lifecycle,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum EventNotificationDisposition {
    Applied,
    Duplicate,
    Repair,
}

struct IncomingEffect {
    outgoing: Vec<Message>,
    repair_events: bool,
}

#[cfg(unix)]
struct ProcessSignals {
    terminate: Arc<AtomicBool>,
    suspend: Arc<AtomicBool>,
}

#[cfg(not(unix))]
struct ProcessSignals;

#[cfg(unix)]
impl ProcessSignals {
    fn install() -> io::Result<Self> {
        use signal_hook::consts::signal::{SIGHUP, SIGINT, SIGTERM, SIGTSTP};

        let terminate = Arc::new(AtomicBool::new(false));
        let suspend = Arc::new(AtomicBool::new(false));
        signal_hook::flag::register(SIGINT, Arc::clone(&terminate))?;
        signal_hook::flag::register(SIGTERM, Arc::clone(&terminate))?;
        signal_hook::flag::register(SIGHUP, Arc::clone(&terminate))?;
        signal_hook::flag::register(SIGTSTP, Arc::clone(&suspend))?;
        Ok(Self { terminate, suspend })
    }

    fn take_termination(&self) -> bool {
        self.terminate.swap(false, Ordering::Relaxed)
    }

    fn take_suspend(&self) -> bool {
        self.suspend.swap(false, Ordering::Relaxed)
    }
}

#[cfg(not(unix))]
impl ProcessSignals {
    fn install() -> io::Result<Self> {
        Ok(Self)
    }

    fn take_termination(&self) -> bool {
        false
    }

    fn take_suspend(&self) -> bool {
        false
    }
}

impl RequestPurpose {
    fn is_refresh(&self) -> bool {
        match self {
            Self::RefreshSessions | Self::RefreshEvents => true,
            Self::Submit(_) => false,
        }
    }
}

impl IpcTransport {
    async fn connect(zeta: &OsStr, cursor: Option<u64>) -> Result<(Self, Bootstrap), BoxError> {
        let mut child = Command::new(zeta)
            .args(IPC_COMMAND_ARGS)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .kill_on_drop(true)
            .spawn()?;
        let Some(input) = child.stdin.take() else {
            return Err(io::Error::other("zeta IPC stdin is unavailable").into());
        };
        let Some(output) = child.stdout.take() else {
            return Err(io::Error::other("zeta IPC stdout is unavailable").into());
        };
        let mut transport = Self {
            child,
            input,
            output: FramedRead::new(output, LinesCodec::new_with_max_length(MAX_FRAME_BYTES)),
            session: new_client_session(),
        };

        let initialize_id = RequestId::from(1_u64);
        send_initialization(
            &mut transport.input,
            &mut transport.session,
            initialize_id.clone(),
        )
        .await?;
        let (result, mut notifications) = receive_response(
            &mut transport.input,
            &mut transport.output,
            &mut transport.session,
            &initialize_id,
        )
        .await?;
        let initialized: InitializeResult = serde_json::from_value(result)?;
        if initialized.runtime.name != "zeta" {
            return Err(io::Error::other(format!(
                "expected zeta runtime, received {}",
                initialized.runtime.name
            ))
            .into());
        }

        let sessions_id = RequestId::from(2_u64);
        send_session_request(
            &mut transport.input,
            &mut transport.session,
            sessions_id.clone(),
            "session.list",
            json!({}),
        )
        .await?;
        let (result, received) = receive_response(
            &mut transport.input,
            &mut transport.output,
            &mut transport.session,
            &sessions_id,
        )
        .await?;
        for event in received {
            notifications.push(event);
        }
        let sessions: SessionsListResult = serde_json::from_value(result)?;

        let events_id = RequestId::from(3_u64);
        send_session_request(
            &mut transport.input,
            &mut transport.session,
            events_id.clone(),
            "events.list",
            events_list_params(cursor),
        )
        .await?;
        let (result, received) = receive_response(
            &mut transport.input,
            &mut transport.output,
            &mut transport.session,
            &events_id,
        )
        .await?;
        for event in received {
            notifications.push(event);
        }
        let events: EventsListResult = serde_json::from_value(result)?;
        Ok((
            transport,
            Bootstrap {
                initialized,
                sessions,
                events,
                notifications,
            },
        ))
    }

    async fn close(mut self) -> Result<(), BoxError> {
        let shutdown_id = RequestId::from("zeta-tui-shutdown");
        send_session_request(
            &mut self.input,
            &mut self.session,
            shutdown_id.clone(),
            "shutdown",
            json!({"reason": "zeta-tui exited"}),
        )
        .await?;
        receive_response(
            &mut self.input,
            &mut self.output,
            &mut self.session,
            &shutdown_id,
        )
        .await?;
        let Self {
            mut child,
            input,
            output,
            session: _,
        } = self;
        drop(output);
        drop(input);
        let status = child.wait().await?;
        if !status.success() {
            return Err(io::Error::other(format!("zeta IPC exited with {status}")).into());
        }
        Ok(())
    }
}

impl ReconnectBackoff {
    fn next_failure(&mut self) -> (usize, Duration) {
        self.failures += 1;
        let exponent = self.failures.saturating_sub(1).min(4);
        let delay_ms = 250_u64.saturating_mul(1_u64 << exponent);
        (self.failures, Duration::from_millis(delay_ms.min(4_000)))
    }

    fn reset(&mut self) {
        self.failures = 0;
    }
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    if let Err(error) = run().await {
        eprintln!("zeta-tui: {error}");
        std::process::exit(1);
    }
}

async fn run() -> Result<(), BoxError> {
    let mut args = env::args_os();
    args.next();
    let zeta: OsString = match args.next() {
        Some(zeta) => zeta,
        None => "zeta".into(),
    };
    if args.next().is_some() {
        return Err(io::Error::other("usage: zeta-tui [PATH_TO_ZETA]").into());
    }

    let mut app = App::connected("unknown".to_owned(), Vec::new(), Vec::new(), None);
    let mut terminal = TerminalSession::start()?;
    terminal.install_panic_hook();
    app.set_keyboard_enhancement(terminal.keyboard_enhancement());
    app.set_terminal_capabilities(terminal.capabilities());
    app.set_reconnecting(1, 0, "Starting zeta IPC".to_owned());
    let mut terminal_events = EventStream::new();
    let mut refresh_interval = tokio::time::interval(REFRESH_INTERVAL);
    refresh_interval.set_missed_tick_behavior(MissedTickBehavior::Skip);
    refresh_interval.tick().await;
    let mut heartbeat_interval = tokio::time::interval(HEARTBEAT_INTERVAL);
    heartbeat_interval.set_missed_tick_behavior(MissedTickBehavior::Skip);
    heartbeat_interval.tick().await;
    let mut lifecycle_interval = tokio::time::interval(LIFECYCLE_INTERVAL);
    lifecycle_interval.set_missed_tick_behavior(MissedTickBehavior::Skip);
    lifecycle_interval.tick().await;
    let process_signals = ProcessSignals::install()?;
    let mut transport = None;
    let mut pending = HashMap::<RequestId, RequestPurpose>::new();
    let mut pending_refreshes = 0;
    let mut replay_submissions = Vec::new();
    let mut next_request_id = 4;
    let mut backoff = ReconnectBackoff::default();
    let mut reconnect_at = Instant::now();

    'application: loop {
        terminal.draw(&mut app)?;

        if transport.is_none() {
            let cursor = app.cursor();
            let connect_at = reconnect_at;
            let connect = async {
                tokio::time::sleep_until(connect_at).await;
                IpcTransport::connect(&zeta, cursor).await
            };
            tokio::pin!(connect);
            let connect_result = loop {
                tokio::select! {
                    terminal_event = terminal_events.next() => {
                        let Some(terminal_event) = terminal_event else {
                            return Err(io::Error::new(
                                io::ErrorKind::UnexpectedEof,
                                "terminal event stream closed",
                            )
                            .into());
                        };
                        let terminal_event = terminal_event?;
                        match app.handle_event(&terminal_event) {
                            AppAction::None => {}
                            AppAction::Quit => break 'application,
                            AppAction::Suspend => suspend_process(&mut terminal)?,
                            AppAction::Submit(objective) => {
                                let (submission_id, _, _) = begin_submission(&mut app, objective);
                                add_replay_submission(&mut replay_submissions, submission_id);
                            }
                            AppAction::Copy(content) => match terminal.copy_to_clipboard(&content) {
                                Ok(()) => app.copy_succeeded(),
                                Err(error) => app.copy_failed(error),
                            },
                        }
                    }
                    _ = lifecycle_interval.tick() => {
                        if process_signals.take_termination() {
                            break 'application;
                        }
                        if process_signals.take_suspend() {
                            suspend_process(&mut terminal)?;
                        }
                    }
                    connect_result = &mut connect => break connect_result,
                }
                terminal.draw(&mut app)?;
            };
            match connect_result {
                Ok((new_transport, bootstrap)) => {
                    app.set_protocol(format!("ipc-v{}", bootstrap.initialized.protocol_version));
                    app.replace_sessions(bootstrap.sessions.sessions);
                    app.append_events(bootstrap.events.events, bootstrap.events.next_cursor);
                    let mut repair_events = false;
                    for event in bootstrap.notifications {
                        let disposition = apply_event_notification(&mut app, event);
                        if disposition == EventNotificationDisposition::Repair {
                            repair_events = true;
                        }
                    }
                    next_request_id = 4;
                    pending.clear();
                    pending_refreshes = 0;
                    transport = Some(new_transport);
                    let replay_result = replay_pending_submissions(
                        &app,
                        transport
                            .as_mut()
                            .expect("connected state owns a transport"),
                        &mut replay_submissions,
                        &mut pending,
                        &mut next_request_id,
                    )
                    .await;
                    let replay_result = match replay_result {
                        Ok(()) if repair_events => {
                            let request_id = RequestId::from(next_request_id);
                            next_request_id += 1;
                            let transport = transport
                                .as_mut()
                                .expect("connected state owns a transport");
                            let result = send_session_request(
                                &mut transport.input,
                                &mut transport.session,
                                request_id.clone(),
                                "events.list",
                                events_list_params(app.cursor()),
                            )
                            .await;
                            match result {
                                Ok(()) => {
                                    let replaced =
                                        pending.insert(request_id, RequestPurpose::RefreshEvents);
                                    debug_assert!(replaced.is_none());
                                    pending_refreshes = 1;
                                    Ok(())
                                }
                                Err(error) => Err(error),
                            }
                        }
                        Ok(()) => Ok(()),
                        Err(error) => Err(error),
                    };
                    match replay_result {
                        Ok(()) => {
                            app.set_connected();
                            backoff.reset();
                            refresh_interval.reset();
                            heartbeat_interval.reset();
                        }
                        Err(error) => {
                            reconnect_at = begin_reconnect(
                                &mut app,
                                error.to_string(),
                                &mut transport,
                                &mut pending,
                                &mut pending_refreshes,
                                &mut replay_submissions,
                                &mut backoff,
                            );
                        }
                    }
                }
                Err(error) => {
                    reconnect_at = begin_reconnect(
                        &mut app,
                        error.to_string(),
                        &mut transport,
                        &mut pending,
                        &mut pending_refreshes,
                        &mut replay_submissions,
                        &mut backoff,
                    );
                }
            }
            continue;
        }

        let loop_event = {
            let current_transport = transport
                .as_mut()
                .expect("connected state owns a transport");
            tokio::select! {
                terminal_event = terminal_events.next() => LoopEvent::Terminal(terminal_event),
                line = current_transport.output.next() => LoopEvent::Ipc(line),
                _ = refresh_interval.tick(), if pending_refreshes == 0 => LoopEvent::Refresh,
                _ = heartbeat_interval.tick() => LoopEvent::Heartbeat,
                _ = lifecycle_interval.tick() => LoopEvent::Lifecycle,
            }
        };
        let mut transport_error = None;
        match loop_event {
            LoopEvent::Terminal(terminal_event) => {
                let Some(terminal_event) = terminal_event else {
                    return Err(io::Error::new(
                        io::ErrorKind::UnexpectedEof,
                        "terminal event stream closed",
                    )
                    .into());
                };
                let terminal_event = terminal_event?;
                match app.handle_event(&terminal_event) {
                    AppAction::None => {}
                    AppAction::Quit => break,
                    AppAction::Suspend => suspend_process(&mut terminal)?,
                    AppAction::Submit(objective) => {
                        let (idempotency_key, method, params) =
                            begin_submission(&mut app, objective);
                        let request_id = RequestId::from(next_request_id);
                        next_request_id += 1;
                        let transport = transport
                            .as_mut()
                            .expect("connected state owns a transport");
                        if let Err(error) = send_session_request(
                            &mut transport.input,
                            &mut transport.session,
                            request_id.clone(),
                            method,
                            params,
                        )
                        .await
                        {
                            add_replay_submission(&mut replay_submissions, idempotency_key.clone());
                            transport_error = Some(error.to_string());
                        } else {
                            let replaced =
                                pending.insert(request_id, RequestPurpose::Submit(idempotency_key));
                            debug_assert!(replaced.is_none());
                        }
                    }
                    AppAction::Copy(content) => match terminal.copy_to_clipboard(&content) {
                        Ok(()) => app.copy_succeeded(),
                        Err(error) => app.copy_failed(error),
                    },
                }
            }
            LoopEvent::Ipc(line) => {
                let Some(line) = line else {
                    transport_error = Some("zeta IPC closed".to_owned());
                    if let Some(error) = transport_error {
                        reconnect_at = begin_reconnect(
                            &mut app,
                            error,
                            &mut transport,
                            &mut pending,
                            &mut pending_refreshes,
                            &mut replay_submissions,
                            &mut backoff,
                        );
                    }
                    continue;
                };
                match line {
                    Ok(line) => {
                        let transport = transport
                            .as_mut()
                            .expect("connected state owns a transport");
                        let result = apply_incoming_line(
                            &mut app,
                            &line,
                            &mut transport.session,
                            &mut pending,
                            &mut pending_refreshes,
                        );
                        match result {
                            Ok(effect) => {
                                for message in effect.outgoing {
                                    if let Err(error) =
                                        send_message(&mut transport.input, &message).await
                                    {
                                        transport_error = Some(error.to_string());
                                        break;
                                    }
                                }
                                if effect.repair_events && !event_refresh_pending(&pending) {
                                    let request_id = RequestId::from(next_request_id);
                                    next_request_id += 1;
                                    let result = send_session_request(
                                        &mut transport.input,
                                        &mut transport.session,
                                        request_id.clone(),
                                        "events.list",
                                        events_list_params(app.cursor()),
                                    )
                                    .await;
                                    match result {
                                        Ok(()) => {
                                            let replaced = pending
                                                .insert(request_id, RequestPurpose::RefreshEvents);
                                            debug_assert!(replaced.is_none());
                                            pending_refreshes += 1;
                                        }
                                        Err(error) => {
                                            transport_error = Some(error.to_string());
                                        }
                                    }
                                }
                            }
                            Err(error) => transport_error = Some(error.to_string()),
                        }
                    }
                    Err(error) => transport_error = Some(error.to_string()),
                }
            }
            LoopEvent::Refresh => {
                app.advance_animation();
                let request_id = RequestId::from(next_request_id);
                next_request_id += 1;
                let transport = transport
                    .as_mut()
                    .expect("connected state owns a transport");
                let sessions_result = send_session_request(
                    &mut transport.input,
                    &mut transport.session,
                    request_id.clone(),
                    "session.list",
                    json!({}),
                )
                .await;
                match sessions_result {
                    Ok(()) => {
                        let replaced = pending.insert(request_id, RequestPurpose::RefreshSessions);
                        debug_assert!(replaced.is_none());
                        pending_refreshes = 1;

                        let request_id = RequestId::from(next_request_id);
                        next_request_id += 1;
                        let events_result = send_session_request(
                            &mut transport.input,
                            &mut transport.session,
                            request_id.clone(),
                            "events.list",
                            events_list_params(app.cursor()),
                        )
                        .await;
                        match events_result {
                            Ok(()) => {
                                let replaced =
                                    pending.insert(request_id, RequestPurpose::RefreshEvents);
                                debug_assert!(replaced.is_none());
                                pending_refreshes = 2;
                            }
                            Err(error) => transport_error = Some(error.to_string()),
                        }
                    }
                    Err(error) => transport_error = Some(error.to_string()),
                }
            }
            LoopEvent::Heartbeat => {
                let request_id = RequestId::from(next_request_id);
                next_request_id += 1;
                let transport = transport
                    .as_mut()
                    .expect("connected state owns a transport");
                let actions = transport.session.on_tick(request_id);
                for action in actions {
                    match action {
                        Action::Send(message) => {
                            if let Err(error) = send_message(&mut transport.input, &message).await {
                                transport_error = Some(error.to_string());
                                break;
                            }
                        }
                        Action::Violation(error) => {
                            transport_error = Some(error.to_string());
                            break;
                        }
                        Action::Close { reason } => {
                            transport_error =
                                Some(reason.unwrap_or_else(|| "the IPC session closed".to_owned()));
                            break;
                        }
                        Action::HandleRequest(_)
                        | Action::HandleNotification(_)
                        | Action::RequestResolved(_) => {
                            transport_error =
                                Some("the heartbeat produced an unexpected IPC action".to_owned());
                            break;
                        }
                    }
                }
            }
            LoopEvent::Lifecycle => {
                if process_signals.take_termination() {
                    break;
                }
                if process_signals.take_suspend() {
                    suspend_process(&mut terminal)?;
                }
            }
        }
        if let Some(error) = transport_error {
            reconnect_at = begin_reconnect(
                &mut app,
                error,
                &mut transport,
                &mut pending,
                &mut pending_refreshes,
                &mut replay_submissions,
                &mut backoff,
            );
        }
    }

    terminal.restore()?;
    if let Some(transport) = transport {
        transport.close().await?;
    }
    Ok(())
}

fn suspend_process(terminal: &mut TerminalSession) -> Result<(), BoxError> {
    terminal.restore()?;
    #[cfg(unix)]
    signal_hook::low_level::raise(signal_hook::consts::signal::SIGSTOP)?;
    terminal.resume()?;
    Ok(())
}

fn begin_submission(app: &mut App, objective: String) -> (String, &'static str, Value) {
    let idempotency_key = Uuid::new_v4().to_string();
    let attached_session_id = app.attached_session_id().map(str::to_owned);
    app.submission_started(idempotency_key.clone(), objective.clone());
    let (method, params) =
        session_message_request(attached_session_id.as_deref(), &objective, &idempotency_key);
    (idempotency_key, method, params)
}

async fn replay_pending_submissions(
    app: &App,
    transport: &mut IpcTransport,
    replay_submissions: &mut Vec<String>,
    pending: &mut HashMap<RequestId, RequestPurpose>,
    next_request_id: &mut u64,
) -> Result<(), BoxError> {
    while let Some(submission_id) = replay_submissions.first().cloned() {
        let request = app.submission_for_replay(&submission_id);
        let Some((session_id, message)) = request else {
            replay_submissions.remove(0);
            continue;
        };
        let (method, params) =
            session_message_request(session_id.as_deref(), &message, &submission_id);
        let request_id = RequestId::from(*next_request_id);
        *next_request_id += 1;
        send_session_request(
            &mut transport.input,
            &mut transport.session,
            request_id.clone(),
            method,
            params,
        )
        .await?;
        let replaced = pending.insert(request_id, RequestPurpose::Submit(submission_id));
        debug_assert!(replaced.is_none());
        replay_submissions.remove(0);
    }
    Ok(())
}

fn begin_reconnect(
    app: &mut App,
    error: String,
    transport: &mut Option<IpcTransport>,
    pending: &mut HashMap<RequestId, RequestPurpose>,
    pending_refreshes: &mut usize,
    replay_submissions: &mut Vec<String>,
    backoff: &mut ReconnectBackoff,
) -> Instant {
    for (_, purpose) in pending.drain() {
        match purpose {
            RequestPurpose::Submit(submission_id) => {
                add_replay_submission(replay_submissions, submission_id);
            }
            RequestPurpose::RefreshSessions | RequestPurpose::RefreshEvents => {}
        }
    }
    *pending_refreshes = 0;
    transport.take();
    let (attempt, delay) = backoff.next_failure();
    let retry_delay_ms = u64::try_from(delay.as_millis()).unwrap_or(u64::MAX);
    app.set_reconnecting(attempt, retry_delay_ms, error);
    Instant::now() + delay
}

fn add_replay_submission(replay_submissions: &mut Vec<String>, submission_id: String) {
    if replay_submissions.contains(&submission_id) {
        return;
    }
    replay_submissions.push(submission_id);
}

fn event_refresh_pending(pending: &HashMap<RequestId, RequestPurpose>) -> bool {
    for purpose in pending.values() {
        if purpose == &RequestPurpose::RefreshEvents {
            return true;
        }
    }
    false
}

fn apply_incoming_line(
    app: &mut App,
    line: &str,
    session: &mut IpcSession,
    pending: &mut HashMap<RequestId, RequestPurpose>,
    pending_refreshes: &mut usize,
) -> Result<IncomingEffect, BoxError> {
    let message = Message::parse_str(line)?;
    let actions = session.receive(message);
    let mut effect = IncomingEffect {
        outgoing: Vec::new(),
        repair_events: false,
    };
    for action in actions {
        match action {
            Action::Send(message) => effect.outgoing.push(message),
            Action::HandleNotification(notification) => {
                let notification: EventNotification =
                    serde_json::from_value(Value::Object(notification.params))?;
                let disposition = apply_event_notification(app, notification.event);
                if disposition == EventNotificationDisposition::Repair {
                    effect.repair_events = true;
                }
            }
            Action::RequestResolved(resolved) => {
                apply_resolved_request(app, resolved, pending, pending_refreshes)?;
            }
            Action::Violation(error) => return Err(error.into()),
            Action::Close { reason } => {
                let reason = reason.unwrap_or_else(|| "the IPC session closed".to_owned());
                return Err(io::Error::other(reason).into());
            }
            Action::HandleRequest(request) => {
                return Err(io::Error::other(format!(
                    "the client cannot handle method {:?}",
                    request.method
                ))
                .into());
            }
        }
    }
    Ok(effect)
}

fn apply_resolved_request(
    app: &mut App,
    resolved: ResolvedRequest,
    pending: &mut HashMap<RequestId, RequestPurpose>,
    pending_refreshes: &mut usize,
) -> Result<(), BoxError> {
    let ResolvedRequest {
        id,
        method,
        outcome,
    } = resolved;
    let Some(purpose) = pending.remove(&id) else {
        if method == "ping" {
            return Ok(());
        }
        return Err(io::Error::other(format!(
            "received a response for request {id} without UI metadata"
        ))
        .into());
    };
    if purpose.is_refresh() {
        *pending_refreshes = pending_refreshes.saturating_sub(1);
    }
    match outcome {
        Ok(result) => {
            if let Err(error) = apply_response(app, &purpose, result) {
                match &purpose {
                    RequestPurpose::Submit(submission_id) => {
                        app.submission_failed(submission_id, &error.to_string());
                    }
                    RequestPurpose::RefreshSessions | RequestPurpose::RefreshEvents => {
                        return Err(error);
                    }
                }
            }
        }
        Err(error) => {
            let ErrorObject {
                code,
                message,
                data,
            } = error;
            apply_failure(app, purpose, code, &message, data)?;
        }
    }
    Ok(())
}

fn apply_event_notification(app: &mut App, event: Event) -> EventNotificationDisposition {
    let Some(cursor) = event.cursor() else {
        return EventNotificationDisposition::Repair;
    };
    let Some(current) = app.cursor() else {
        return EventNotificationDisposition::Repair;
    };
    if cursor.0 <= current {
        return EventNotificationDisposition::Duplicate;
    }
    if current.checked_add(1) != Some(cursor.0) {
        return EventNotificationDisposition::Repair;
    }
    app.append_events(vec![event], Some(cursor));
    EventNotificationDisposition::Applied
}

fn new_client_session() -> IpcSession {
    let params = InitializeParams {
        protocol_versions: vec![0],
        peer: PeerIdentity::new("zeta-tui", env!("CARGO_PKG_VERSION")),
        roles: vec![Role::Client],
        event_types: None,
        methods: None,
        heartbeat_seconds: Some(10.0),
        max_in_flight: Some(64),
    };
    IpcSession::peer(params, ShutdownDirection::LocalSupervisesRemote)
}

async fn send_initialization(
    input: &mut ChildStdin,
    session: &mut IpcSession,
    id: RequestId,
) -> Result<(), BoxError> {
    let actions = session.initialize(id)?;
    let message = outgoing_message(actions)?;
    send_message(input, &message).await
}

async fn send_session_request(
    input: &mut ChildStdin,
    session: &mut IpcSession,
    id: RequestId,
    method: &str,
    params: Value,
) -> Result<(), BoxError> {
    let Value::Object(params) = params else {
        return Err(io::Error::other("IPC request parameters must be an object").into());
    };
    let actions = session.send_request(id, method, params)?;
    let message = outgoing_message(actions)?;
    send_message(input, &message).await
}

fn outgoing_message(actions: Vec<Action>) -> Result<Message, BoxError> {
    let [Action::Send(message)] = actions.as_slice() else {
        return Err(io::Error::other("an outgoing IPC request must emit one message").into());
    };
    Ok(message.clone())
}

async fn send_message(input: &mut ChildStdin, message: &Message) -> Result<(), BoxError> {
    let mut line = message.to_json().into_bytes();
    line.push(b'\n');
    input.write_all(&line).await?;
    input.flush().await?;
    Ok(())
}

async fn receive_response(
    input: &mut ChildStdin,
    output: &mut FramedRead<ChildStdout, LinesCodec>,
    session: &mut IpcSession,
    expected_id: &RequestId,
) -> Result<(Value, Vec<Event>), BoxError> {
    let mut notifications = Vec::new();
    while let Some(line) = output.next().await {
        let line = line?;
        let message = Message::parse_str(&line)?;
        let actions = session.receive(message);
        for action in actions {
            match action {
                Action::Send(message) => send_message(input, &message).await?,
                Action::HandleNotification(notification) => {
                    let notification: EventNotification =
                        serde_json::from_value(Value::Object(notification.params))?;
                    notifications.push(notification.event);
                }
                Action::RequestResolved(resolved) => {
                    let ResolvedRequest {
                        id,
                        method: _,
                        outcome,
                    } = resolved;
                    if &id != expected_id {
                        continue;
                    }
                    return match outcome {
                        Ok(result) => Ok((result, notifications)),
                        Err(error) => Err(ipc_error(error)),
                    };
                }
                Action::Violation(error) => return Err(error.into()),
                Action::Close { reason } => {
                    let reason = reason.unwrap_or_else(|| "the IPC session closed".to_owned());
                    return Err(io::Error::other(reason).into());
                }
                Action::HandleRequest(request) => {
                    return Err(io::Error::other(format!(
                        "the client cannot handle method {:?}",
                        request.method
                    ))
                    .into());
                }
            }
        }
    }
    Err(io::Error::new(io::ErrorKind::UnexpectedEof, "zeta IPC closed").into())
}

fn ipc_error(error: ErrorObject) -> BoxError {
    let ErrorObject {
        code,
        message,
        data,
    } = error;
    let data = match data {
        Some(data) => format!(" ({data})"),
        None => String::new(),
    };
    io::Error::other(format!("IPC error {code}: {message}{data}")).into()
}

fn submission_error_message(message: &str, data: Option<&Value>) -> String {
    let message = message.trim();
    let detail = match data {
        Some(Value::String(detail)) => Some(detail.as_str()),
        Some(Value::Object(data)) => {
            let detail = data.get("detail").and_then(Value::as_str);
            match detail {
                Some(detail) => Some(detail),
                None => data.get("message").and_then(Value::as_str),
            }
        }
        Some(Value::Null)
        | Some(Value::Bool(_))
        | Some(Value::Number(_))
        | Some(Value::Array(_))
        | None => None,
    };
    let Some(detail) = detail else {
        return message.to_owned();
    };
    let detail = detail.trim();
    if detail.is_empty() || detail.eq_ignore_ascii_case(message) {
        return message.to_owned();
    }
    format!("{message}: {detail}")
}

fn session_message_request(
    session_id: Option<&str>,
    message: &str,
    idempotency_key: &str,
) -> (&'static str, Value) {
    match session_id {
        Some(session_id) => (
            "session.send",
            json!({
                "session_id": session_id,
                "message": message,
                "idempotency_key": idempotency_key
            }),
        ),
        None => (
            "session.start",
            json!({
                "message": message,
                "idempotency_key": idempotency_key
            }),
        ),
    }
}

fn events_list_params(cursor: Option<u64>) -> Value {
    let mut params = json!({"limit": 200});
    if let Some(cursor) = cursor {
        params["after_cursor"] = json!(cursor);
    }
    params
}

fn apply_response(app: &mut App, purpose: &RequestPurpose, result: Value) -> Result<(), BoxError> {
    match purpose {
        RequestPurpose::RefreshSessions => {
            let sessions: SessionsListResult = serde_json::from_value(result)?;
            app.replace_sessions(sessions.sessions);
        }
        RequestPurpose::RefreshEvents => {
            let listed: EventsListResult = serde_json::from_value(result)?;
            app.append_events(listed.events, listed.next_cursor);
        }
        RequestPurpose::Submit(submission_id) => {
            let submitted: SubmitResult = serde_json::from_value(result)?;
            if submitted.status != "queued" {
                return Err(io::Error::other(format!(
                    "unexpected submission status {}",
                    submitted.status
                ))
                .into());
            }
            app.submission_queued(submission_id, &submitted.event_id, &submitted.session_id);
        }
    }
    Ok(())
}

fn apply_failure(
    app: &mut App,
    purpose: RequestPurpose,
    code: i64,
    message: &str,
    data: Option<Value>,
) -> Result<(), BoxError> {
    match purpose {
        RequestPurpose::Submit(submission_id) => {
            let error = submission_error_message(message, data.as_ref());
            app.submission_failed(&submission_id, &error);
            Ok(())
        }
        RequestPurpose::RefreshSessions | RequestPurpose::RefreshEvents => {
            Err(ipc_error(ErrorObject::new(code, message, data)))
        }
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;
    use zeta_ipc::{Action, InitializeParams, Message, RequestId, Role, SuccessResponse};

    use crossterm::event::{Event as TerminalEvent, KeyCode, KeyEvent, KeyModifiers};

    use super::{
        EventNotificationDisposition, IPC_COMMAND_ARGS, ReconnectBackoff, RequestPurpose,
        apply_event_notification, apply_failure, apply_incoming_line, apply_response,
        begin_reconnect, events_list_params, new_client_session, session_message_request,
    };
    use crate::app::{App, AppAction};
    use crate::wire::{Cursor, Event};

    fn initialized_client_session() -> zeta_ipc::Session {
        let mut session = new_client_session();
        let id = RequestId::from(1_u64);
        session
            .initialize(id.clone())
            .expect("client profile should initialize");
        let result = json!({
            "protocol_version": 0,
            "runtime": {"name": "zeta", "version": "0.1.0"},
            "roles": ["client"],
            "config": {},
            "heartbeat_seconds": 10,
            "max_in_flight": 64
        });
        let actions = session.receive(Message::Success(SuccessResponse::new(id, result)));
        let [Action::RequestResolved(_)] = actions.as_slice() else {
            panic!("initialization should resolve");
        };
        session
    }

    fn notification_event(cursor: u64) -> Event {
        serde_json::from_value(json!({
            "id": format!("evt_{cursor}"),
            "type": "zeta.user_message",
            "source": "user",
            "payload": {"content": format!("message {cursor}")},
            "idempotency_key": null,
            "caused_by": null,
            "session_id": "session_1",
            "run_id": "run_1",
            "turn_id": null,
            "timestamp_ms": cursor,
            "cursor": cursor
        }))
        .expect("notification event should parse")
    }

    #[test]
    fn client_initialization_uses_the_shared_client_role() {
        let mut session = new_client_session();

        let actions = session
            .initialize(RequestId::from(1_u64))
            .expect("client profile should initialize");

        let [Action::Send(Message::Request(request))] = actions.as_slice() else {
            panic!("initialization must emit one request");
        };
        assert_eq!(request.method, "initialize");
        let params: InitializeParams =
            serde_json::from_value(serde_json::Value::Object(request.params.clone()))
                .expect("initialization parameters should parse");
        assert_eq!(params.roles, vec![Role::Client]);
        assert_eq!(params.peer.name, "zeta-tui");
    }

    #[test]
    fn tui_spawns_the_unified_ipc_command() {
        assert_eq!(IPC_COMMAND_ARGS, ["ipc", "stdio"]);
    }

    #[test]
    fn event_notifications_apply_contiguous_cursors_and_repair_gaps() {
        let mut app = App::connected("0".to_owned(), Vec::new(), Vec::new(), Some(Cursor(4)));

        assert_eq!(
            apply_event_notification(&mut app, notification_event(5)),
            EventNotificationDisposition::Applied
        );
        assert_eq!(app.cursor(), Some(5));
        assert_eq!(
            apply_event_notification(&mut app, notification_event(5)),
            EventNotificationDisposition::Duplicate
        );
        assert_eq!(
            apply_event_notification(&mut app, notification_event(7)),
            EventNotificationDisposition::Repair
        );
        assert_eq!(app.cursor(), Some(5));
    }

    #[test]
    fn events_list_continues_after_the_latest_cursor() {
        assert_eq!(events_list_params(None), json!({"limit": 200}));
        assert_eq!(
            events_list_params(Some(42)),
            json!({"limit": 200, "after_cursor": 42})
        );
    }

    #[test]
    fn refresh_responses_apply_independently_of_response_order() {
        let mut app = App::connected("0.1".to_owned(), Vec::new(), Vec::new(), None);

        apply_response(
            &mut app,
            &RequestPurpose::RefreshEvents,
            json!({"events": [], "next_cursor": 42}),
        )
        .expect("event refresh should apply");
        apply_response(
            &mut app,
            &RequestPurpose::RefreshSessions,
            json!({
                "sessions": [{
                    "session_id": "session_123",
                    "agent_id": "zeta.master",
                    "status": "queued"
                }]
            }),
        )
        .expect("session refresh should apply");

        assert_eq!(app.cursor(), Some(42));
        assert_eq!(app.selected_session_id(), Some("session_123"));
    }

    #[test]
    fn shared_session_correlates_out_of_order_refresh_responses() {
        let mut app = App::connected("0".to_owned(), Vec::new(), Vec::new(), None);
        let mut session = initialized_client_session();
        let sessions_id = RequestId::from(30_u64);
        let events_id = RequestId::from(31_u64);
        session
            .send_request(sessions_id.clone(), "session.list", serde_json::Map::new())
            .expect("session refresh should become pending");
        session
            .send_request(
                events_id.clone(),
                "events.list",
                json!({"limit": 200}).as_object().unwrap().clone(),
            )
            .expect("event refresh should become pending");
        let mut pending = std::collections::HashMap::from([
            (sessions_id, RequestPurpose::RefreshSessions),
            (events_id, RequestPurpose::RefreshEvents),
        ]);
        let mut pending_refreshes = 2;

        let events = json!({
            "jsonrpc": "2.0",
            "id": 31,
            "result": {"events": [], "next_cursor": 42}
        });
        apply_incoming_line(
            &mut app,
            &events.to_string(),
            &mut session,
            &mut pending,
            &mut pending_refreshes,
        )
        .expect("event response should resolve first");
        let sessions = json!({
            "jsonrpc": "2.0",
            "id": 30,
            "result": {"sessions": [{
                "session_id": "session_123",
                "agent_id": "zeta.master",
                "status": "queued"
            }]}
        });
        apply_incoming_line(
            &mut app,
            &sessions.to_string(),
            &mut session,
            &mut pending,
            &mut pending_refreshes,
        )
        .expect("session response should resolve second");

        assert_eq!(app.cursor(), Some(42));
        assert_eq!(app.selected_session_id(), Some("session_123"));
        assert_eq!(pending_refreshes, 0);
        assert!(pending.is_empty());
    }

    #[test]
    fn shared_session_delivers_event_notifications_and_flags_cursor_gaps() {
        let mut app = App::connected("0".to_owned(), Vec::new(), Vec::new(), Some(Cursor(4)));
        let mut session = initialized_client_session();
        let mut pending = std::collections::HashMap::new();
        let mut pending_refreshes = 0;
        let contiguous = json!({
            "jsonrpc": "2.0",
            "method": "event",
            "params": {"event": serde_json::to_value(notification_event(5)).unwrap()}
        });

        let effect = apply_incoming_line(
            &mut app,
            &contiguous.to_string(),
            &mut session,
            &mut pending,
            &mut pending_refreshes,
        )
        .expect("contiguous notification should apply");
        assert_eq!(app.cursor(), Some(5));
        assert!(!effect.repair_events);
        assert!(effect.outgoing.is_empty());

        let gap = json!({
            "jsonrpc": "2.0",
            "method": "event",
            "params": {"event": serde_json::to_value(notification_event(7)).unwrap()}
        });
        let effect = apply_incoming_line(
            &mut app,
            &gap.to_string(),
            &mut session,
            &mut pending,
            &mut pending_refreshes,
        )
        .expect("gap notification should request repair");
        assert!(effect.repair_events);
        assert_eq!(app.cursor(), Some(5));
    }

    #[test]
    fn session_message_starts_without_a_selected_session() {
        let (method, params) = session_message_request(None, "hello", "message-1");

        assert_eq!(method, "session.start");
        assert_eq!(
            params,
            json!({"message": "hello", "idempotency_key": "message-1"})
        );
    }

    #[test]
    fn submit_response_moves_a_starting_submission_into_its_session() {
        let mut app = App::connected("0.1".to_owned(), Vec::new(), Vec::new(), None);
        app.submission_started("message-1".to_owned(), "hello".to_owned());

        apply_response(
            &mut app,
            &RequestPurpose::Submit("message-1".to_owned()),
            json!({
                "event_id": "evt_1",
                "session_id": "session_1",
                "status": "queued"
            }),
        )
        .expect("submit response should apply");

        assert_eq!(app.attached_session_id(), Some("session_1"));
    }

    #[test]
    fn submit_ipc_failure_is_retryable_while_refresh_failure_remains_fatal() {
        let mut app = App::connected("0.1".to_owned(), Vec::new(), Vec::new(), None);
        app.submission_started("message-1".to_owned(), "hello".to_owned());

        apply_failure(
            &mut app,
            RequestPurpose::Submit("message-1".to_owned()),
            -32_602,
            "session unavailable",
            None,
        )
        .expect("submit failure should stay inside the app");
        let enter = TerminalEvent::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(
            app.handle_event(&enter),
            AppAction::Submit("hello".to_owned())
        );

        assert!(
            apply_failure(
                &mut app,
                RequestPurpose::RefreshEvents,
                -32_603,
                "internal error",
                None,
            )
            .is_err()
        );
    }

    #[test]
    fn submission_error_prefers_human_detail_and_omits_structural_data() {
        assert_eq!(
            super::submission_error_message(
                "Session unavailable",
                Some(&json!({"detail": "Agent is not accepting messages"})),
            ),
            "Session unavailable: Agent is not accepting messages"
        );
        assert_eq!(
            super::submission_error_message(
                "Session unavailable",
                Some(&json!({"session_id": "session_1", "retryable": false})),
            ),
            "Session unavailable"
        );
    }

    #[test]
    fn session_message_addresses_the_selected_session() {
        let (method, params) =
            session_message_request(Some("session_123"), "continue", "message-2");

        assert_eq!(method, "session.send");
        assert_eq!(
            params,
            json!({
                "session_id": "session_123",
                "message": "continue",
                "idempotency_key": "message-2"
            })
        );
    }

    #[test]
    fn reconnect_backoff_is_bounded_and_resets_after_success() {
        let mut backoff = ReconnectBackoff::default();
        let mut attempts = Vec::new();
        for _ in 0..8 {
            attempts.push(backoff.next_failure());
        }
        assert_eq!(attempts[0], (1, std::time::Duration::from_millis(250)));
        assert_eq!(attempts[1], (2, std::time::Duration::from_millis(500)));
        assert_eq!(attempts[4], (5, std::time::Duration::from_secs(4)));
        assert_eq!(attempts[7], (8, std::time::Duration::from_secs(4)));

        backoff.reset();
        assert_eq!(
            backoff.next_failure(),
            (1, std::time::Duration::from_millis(250))
        );
    }

    #[test]
    fn transport_failure_retains_each_in_flight_submission_for_replay() {
        let mut app = App::connected("0.1".to_owned(), Vec::new(), Vec::new(), None);
        app.submission_started("message-1".to_owned(), "first".to_owned());
        app.submission_started("message-2".to_owned(), "second".to_owned());
        let mut pending = std::collections::HashMap::from([
            (
                RequestId::from(10_u64),
                RequestPurpose::Submit("message-1".to_owned()),
            ),
            (
                RequestId::from(11_u64),
                RequestPurpose::Submit("message-2".to_owned()),
            ),
            (RequestId::from(12_u64), RequestPurpose::RefreshEvents),
        ]);
        let mut transport = None;
        let mut pending_refreshes = 1;
        let mut replay = vec!["message-1".to_owned()];
        let mut backoff = ReconnectBackoff::default();

        let reconnect_at = begin_reconnect(
            &mut app,
            "broken pipe".to_owned(),
            &mut transport,
            &mut pending,
            &mut pending_refreshes,
            &mut replay,
            &mut backoff,
        );

        assert!(reconnect_at > tokio::time::Instant::now());
        replay.sort();
        assert_eq!(replay, vec!["message-1", "message-2"]);
        assert!(pending.is_empty());
        assert_eq!(pending_refreshes, 0);
    }

    #[test]
    fn durable_event_reconciliation_suppresses_submission_replay() {
        let mut app = App::connected("0.1".to_owned(), Vec::new(), Vec::new(), None);
        app.submission_started("message-1".to_owned(), "hello".to_owned());
        assert_eq!(
            app.submission_for_replay("message-1"),
            Some((None, "hello".to_owned()))
        );
        let event: Event = serde_json::from_value(json!({
            "id": "evt_1",
            "type": "session.message.requested",
            "source": "user",
            "payload": {"message": "hello"},
            "idempotency_key": "session.start:message-1",
            "caused_by": null,
            "session_id": "session_1",
            "run_id": "run_1",
            "turn_id": null,
            "timestamp_ms": 1,
            "cursor": 1
        }))
        .expect("event should parse");

        app.append_events(vec![event], Some(Cursor(1)));

        assert_eq!(app.submission_for_replay("message-1"), None);
    }

    #[test]
    fn malformed_and_failed_refresh_responses_enter_the_recovery_path() {
        let mut app = App::connected("0.1".to_owned(), Vec::new(), Vec::new(), None);
        let mut pending = std::collections::HashMap::new();
        let mut pending_refreshes = 0;
        let mut session = initialized_client_session();
        assert!(
            apply_incoming_line(
                &mut app,
                "not json",
                &mut session,
                &mut pending,
                &mut pending_refreshes,
            )
            .is_err()
        );

        let request_id = RequestId::from(20_u64);
        session
            .send_request(request_id.clone(), "session.list", serde_json::Map::new())
            .expect("session refresh should become pending");
        pending.insert(request_id, RequestPurpose::RefreshSessions);
        pending_refreshes = 1;
        let failure = json!({
            "jsonrpc": "2.0",
            "id": 20,
            "error": {"code": -32603, "message": "restart me"}
        });
        assert!(
            apply_incoming_line(
                &mut app,
                &failure.to_string(),
                &mut session,
                &mut pending,
                &mut pending_refreshes,
            )
            .is_err()
        );
        assert_eq!(pending_refreshes, 0);
    }
}
