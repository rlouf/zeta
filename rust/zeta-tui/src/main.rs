mod app;
mod wire;

use std::collections::HashMap;
use std::env;
use std::error::Error;
use std::ffi::{OsStr, OsString};
use std::io;
use std::process::Stdio;
use std::time::Duration;

use crossterm::event::{Event as TerminalEvent, EventStream};
use futures_util::StreamExt;
use serde_json::{Value, json};
use tokio::io::AsyncWriteExt;
use tokio::process::{ChildStdin, ChildStdout, Command};
use tokio::time::{Instant, MissedTickBehavior};
use tokio_util::codec::{FramedRead, LinesCodec, LinesCodecError};
use uuid::Uuid;

use crate::app::{App, AppAction, TerminalSession};
use crate::wire::{
    EventsListResult, IncomingMessage, InitializeResult, RequestId, RpcRequest, SessionsListResult,
    SubmitResult,
};

const MAX_JSONRPC_LINE_BYTES: usize = 1024 * 1024;
const REFRESH_INTERVAL: Duration = Duration::from_millis(500);

type BoxError = Box<dyn Error>;

#[derive(Clone, Debug, PartialEq, Eq)]
enum RequestPurpose {
    RefreshSessions,
    RefreshEvents,
    Submit(String),
}

struct RpcTransport {
    child: tokio::process::Child,
    input: ChildStdin,
    output: FramedRead<ChildStdout, LinesCodec>,
}

struct Bootstrap {
    initialized: InitializeResult,
    sessions: SessionsListResult,
    events: EventsListResult,
}

#[derive(Debug, Default, PartialEq, Eq)]
struct ReconnectBackoff {
    failures: usize,
}

enum LoopEvent {
    Terminal(Option<Result<TerminalEvent, io::Error>>),
    Rpc(Option<Result<String, LinesCodecError>>),
    Refresh,
}

impl RequestPurpose {
    fn is_refresh(&self) -> bool {
        match self {
            Self::RefreshSessions | Self::RefreshEvents => true,
            Self::Submit(_) => false,
        }
    }
}

impl RpcTransport {
    async fn connect(zeta: &OsStr, cursor: Option<u64>) -> Result<(Self, Bootstrap), BoxError> {
        let mut child = Command::new(zeta)
            .args(["rpc", "stdio"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .kill_on_drop(true)
            .spawn()?;
        let Some(input) = child.stdin.take() else {
            return Err(io::Error::other("zeta RPC stdin is unavailable").into());
        };
        let Some(output) = child.stdout.take() else {
            return Err(io::Error::other("zeta RPC stdout is unavailable").into());
        };
        let mut transport = Self {
            child,
            input,
            output: FramedRead::new(
                output,
                LinesCodec::new_with_max_length(MAX_JSONRPC_LINE_BYTES),
            ),
        };

        let initialize_id = RequestId(1);
        send_request(
            &mut transport.input,
            &RpcRequest::new(initialize_id, "initialize", json!({})),
        )
        .await?;
        let result = receive_response(&mut transport.output, initialize_id).await?;
        let initialized: InitializeResult = serde_json::from_value(result)?;
        if initialized.server != "zeta" {
            return Err(io::Error::other(format!(
                "expected zeta server, received {}",
                initialized.server
            ))
            .into());
        }

        let sessions_id = RequestId(2);
        send_request(
            &mut transport.input,
            &RpcRequest::new(sessions_id, "session.list", json!({})),
        )
        .await?;
        let result = receive_response(&mut transport.output, sessions_id).await?;
        let sessions: SessionsListResult = serde_json::from_value(result)?;

        let events_id = RequestId(3);
        send_request(
            &mut transport.input,
            &RpcRequest::new(events_id, "events.list", events_list_params(cursor)),
        )
        .await?;
        let result = receive_response(&mut transport.output, events_id).await?;
        let events: EventsListResult = serde_json::from_value(result)?;
        Ok((
            transport,
            Bootstrap {
                initialized,
                sessions,
                events,
            },
        ))
    }

    async fn close(self) -> Result<(), BoxError> {
        let Self {
            mut child,
            input,
            output,
        } = self;
        drop(output);
        drop(input);
        let status = child.wait().await?;
        if !status.success() {
            return Err(io::Error::other(format!("zeta RPC exited with {status}")).into());
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
    app.set_keyboard_enhancement(terminal.keyboard_enhancement());
    app.set_reconnecting(1, 0, "Starting zeta RPC".to_owned());
    let mut terminal_events = EventStream::new();
    let mut refresh_interval = tokio::time::interval(REFRESH_INTERVAL);
    refresh_interval.set_missed_tick_behavior(MissedTickBehavior::Skip);
    refresh_interval.tick().await;
    let mut transport = None;
    let mut pending = HashMap::<RequestId, RequestPurpose>::new();
    let mut pending_refreshes = 0;
    let mut replay_submissions = Vec::new();
    let mut next_request_id = 4;
    let mut backoff = ReconnectBackoff::default();
    let mut reconnect_at = Instant::now();

    loop {
        terminal.draw(&mut app)?;

        if transport.is_none() {
            let reconnect = tokio::time::sleep_until(reconnect_at);
            tokio::pin!(reconnect);
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
                        AppAction::Quit => break,
                        AppAction::Submit(objective) => {
                            let (submission_id, _, _) = begin_submission(&mut app, objective);
                            add_replay_submission(&mut replay_submissions, submission_id);
                        }
                    }
                }
                _ = &mut reconnect => {
                    match RpcTransport::connect(&zeta, app.cursor()).await {
                        Ok((new_transport, bootstrap)) => {
                            app.set_protocol(bootstrap.initialized.protocol);
                            app.replace_sessions(bootstrap.sessions.sessions);
                            app.append_events(
                                bootstrap.events.events,
                                bootstrap.events.next_cursor,
                            );
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
                            match replay_result {
                                Ok(()) => {
                                    app.set_connected();
                                    backoff.reset();
                                    refresh_interval.reset();
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
                line = current_transport.output.next() => LoopEvent::Rpc(line),
                _ = refresh_interval.tick(), if pending_refreshes == 0 => LoopEvent::Refresh,
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
                    AppAction::Submit(objective) => {
                        let (idempotency_key, method, params) =
                            begin_submission(&mut app, objective);
                        let request_id = RequestId(next_request_id);
                        next_request_id += 1;
                        if let Err(error) = send_request(
                            &mut transport
                                .as_mut()
                                .expect("connected state owns a transport")
                                .input,
                            &RpcRequest::new(request_id, method, params),
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
                }
            }
            LoopEvent::Rpc(line) => {
                let Some(line) = line else {
                    transport_error = Some("zeta RPC closed".to_owned());
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
                        if let Err(error) = apply_incoming_line(
                            &mut app,
                            &line,
                            &mut pending,
                            &mut pending_refreshes,
                        ) {
                            transport_error = Some(error.to_string());
                        }
                    }
                    Err(error) => transport_error = Some(error.to_string()),
                }
            }
            LoopEvent::Refresh => {
                app.advance_animation();
                let request_id = RequestId(next_request_id);
                next_request_id += 1;
                let sessions_result = send_request(
                    &mut transport
                        .as_mut()
                        .expect("connected state owns a transport")
                        .input,
                    &RpcRequest::new(request_id, "session.list", json!({})),
                )
                .await;
                match sessions_result {
                    Ok(()) => {
                        let replaced = pending.insert(request_id, RequestPurpose::RefreshSessions);
                        debug_assert!(replaced.is_none());
                        pending_refreshes = 1;

                        let request_id = RequestId(next_request_id);
                        next_request_id += 1;
                        let events_result = send_request(
                            &mut transport
                                .as_mut()
                                .expect("connected state owns a transport")
                                .input,
                            &RpcRequest::new(
                                request_id,
                                "events.list",
                                events_list_params(app.cursor()),
                            ),
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
    transport: &mut RpcTransport,
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
        let request_id = RequestId(*next_request_id);
        *next_request_id += 1;
        send_request(
            &mut transport.input,
            &RpcRequest::new(request_id, method, params),
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
    transport: &mut Option<RpcTransport>,
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

fn apply_incoming_line(
    app: &mut App,
    line: &str,
    pending: &mut HashMap<RequestId, RequestPurpose>,
    pending_refreshes: &mut usize,
) -> Result<(), BoxError> {
    let message: IncomingMessage = serde_json::from_str(line)?;
    match message {
        IncomingMessage::Success(response) => {
            validate_jsonrpc_version(&response.jsonrpc)?;
            let Some(purpose) = pending.remove(&response.id) else {
                return Err(io::Error::other(format!(
                    "received response for unknown request {}",
                    response.id.0
                ))
                .into());
            };
            if purpose.is_refresh() {
                *pending_refreshes = pending_refreshes.saturating_sub(1);
            }
            if let Err(error) = apply_response(app, &purpose, response.result) {
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
        IncomingMessage::Failure(response) => {
            validate_jsonrpc_version(&response.jsonrpc)?;
            let Some(purpose) = pending.remove(&response.id) else {
                return Err(io::Error::other(format!(
                    "received error for unknown request {}",
                    response.id.0
                ))
                .into());
            };
            if purpose.is_refresh() {
                *pending_refreshes = pending_refreshes.saturating_sub(1);
            }
            apply_failure(
                app,
                purpose,
                response.error.code,
                &response.error.message,
                response.error.data,
            )?;
        }
        IncomingMessage::Notification(notification) => {
            validate_jsonrpc_version(&notification.jsonrpc)?;
            let _notification = (notification.method, notification.params);
        }
    }
    Ok(())
}

async fn send_request(input: &mut ChildStdin, request: &RpcRequest<'_>) -> Result<(), BoxError> {
    let mut line = serde_json::to_vec(request)?;
    line.push(b'\n');
    input.write_all(&line).await?;
    input.flush().await?;
    Ok(())
}

async fn receive_response(
    output: &mut FramedRead<ChildStdout, LinesCodec>,
    expected_id: RequestId,
) -> Result<Value, BoxError> {
    while let Some(line) = output.next().await {
        let line = line?;
        let message: IncomingMessage = serde_json::from_str(&line)?;
        match message {
            IncomingMessage::Success(response) => {
                validate_jsonrpc_version(&response.jsonrpc)?;
                if response.id != expected_id {
                    return Err(io::Error::other(format!(
                        "received response {} while waiting for {}",
                        response.id.0, expected_id.0
                    ))
                    .into());
                }
                return Ok(response.result);
            }
            IncomingMessage::Failure(response) => {
                validate_jsonrpc_version(&response.jsonrpc)?;
                if response.id != expected_id {
                    return Err(io::Error::other(format!(
                        "received error response {} while waiting for {}",
                        response.id.0, expected_id.0
                    ))
                    .into());
                }
                return Err(rpc_error(
                    response.error.code,
                    &response.error.message,
                    response.error.data,
                ));
            }
            IncomingMessage::Notification(notification) => {
                validate_jsonrpc_version(&notification.jsonrpc)?;
                let _ = (notification.method, notification.params);
            }
        }
    }
    Err(io::Error::new(io::ErrorKind::UnexpectedEof, "zeta RPC closed").into())
}

fn rpc_error(code: i64, message: &str, data: Option<Value>) -> BoxError {
    let data = match data {
        Some(data) => format!(" ({data})"),
        None => String::new(),
    };
    io::Error::other(format!("JSON-RPC error {code}: {message}{data}")).into()
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

fn validate_jsonrpc_version(version: &str) -> Result<(), BoxError> {
    if version != "2.0" {
        return Err(io::Error::other(format!("unsupported JSON-RPC version {version}")).into());
    }
    Ok(())
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
            Err(rpc_error(code, message, data))
        }
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use crossterm::event::{Event as TerminalEvent, KeyCode, KeyEvent, KeyModifiers};

    use super::{
        ReconnectBackoff, RequestPurpose, apply_failure, apply_incoming_line, apply_response,
        begin_reconnect, events_list_params, session_message_request,
    };
    use crate::app::{App, AppAction};
    use crate::wire::{Cursor, Event, RequestId};

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
    fn submit_rpc_failure_is_retryable_while_refresh_failure_remains_fatal() {
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
                RequestId(10),
                RequestPurpose::Submit("message-1".to_owned()),
            ),
            (
                RequestId(11),
                RequestPurpose::Submit("message-2".to_owned()),
            ),
            (RequestId(12), RequestPurpose::RefreshEvents),
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
            "event_type": "session.message.requested",
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
        assert!(
            apply_incoming_line(&mut app, "not json", &mut pending, &mut pending_refreshes,)
                .is_err()
        );

        pending.insert(RequestId(20), RequestPurpose::RefreshSessions);
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
                &mut pending,
                &mut pending_refreshes,
            )
            .is_err()
        );
        assert_eq!(pending_refreshes, 0);
    }
}
