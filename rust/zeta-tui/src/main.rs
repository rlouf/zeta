mod app;
mod wire;

use std::collections::HashMap;
use std::env;
use std::error::Error;
use std::io;
use std::process::Stdio;
use std::time::Duration;

use crossterm::event::EventStream;
use futures_util::StreamExt;
use serde_json::{Value, json};
use tokio::io::AsyncWriteExt;
use tokio::process::{ChildStdin, ChildStdout, Command};
use tokio::time::MissedTickBehavior;
use tokio_util::codec::{FramedRead, LinesCodec};
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

impl RequestPurpose {
    fn is_refresh(&self) -> bool {
        match self {
            Self::RefreshSessions | Self::RefreshEvents => true,
            Self::Submit(_) => false,
        }
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
    let zeta = match args.next() {
        Some(zeta) => zeta,
        None => "zeta".into(),
    };
    if args.next().is_some() {
        return Err(io::Error::other("usage: zeta-tui [PATH_TO_ZETA]").into());
    }

    let mut child = Command::new(zeta)
        .args(["rpc", "stdio"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .kill_on_drop(true)
        .spawn()?;
    let Some(mut input) = child.stdin.take() else {
        return Err(io::Error::other("zeta RPC stdin is unavailable").into());
    };
    let Some(output) = child.stdout.take() else {
        return Err(io::Error::other("zeta RPC stdout is unavailable").into());
    };
    let mut output = FramedRead::new(
        output,
        LinesCodec::new_with_max_length(MAX_JSONRPC_LINE_BYTES),
    );

    let initialize_id = RequestId(1);
    send_request(
        &mut input,
        &RpcRequest::new(initialize_id, "initialize", json!({})),
    )
    .await?;
    let result = receive_response(&mut output, initialize_id).await?;
    let initialized: InitializeResult = serde_json::from_value(result)?;
    if initialized.server != "zeta" {
        return Err(io::Error::other(format!(
            "expected zeta server, received {}",
            initialized.server
        ))
        .into());
    }

    let list_id = RequestId(2);
    send_request(
        &mut input,
        &RpcRequest::new(list_id, "session.list", json!({})),
    )
    .await?;
    let result = receive_response(&mut output, list_id).await?;
    let sessions: SessionsListResult = serde_json::from_value(result)?;

    let list_id = RequestId(3);
    send_request(
        &mut input,
        &RpcRequest::new(list_id, "events.list", json!({"limit": 200})),
    )
    .await?;
    let result = receive_response(&mut output, list_id).await?;
    let listed: EventsListResult = serde_json::from_value(result)?;
    let mut app = App::connected(
        initialized.protocol,
        sessions.sessions,
        listed.events,
        listed.next_cursor,
    );
    let mut next_request_id = 4;
    let mut terminal = TerminalSession::start()?;
    app.set_keyboard_enhancement(terminal.keyboard_enhancement());
    let mut terminal_events = EventStream::new();
    let mut refresh_interval = tokio::time::interval(REFRESH_INTERVAL);
    refresh_interval.set_missed_tick_behavior(MissedTickBehavior::Skip);
    refresh_interval.tick().await;
    let mut pending = HashMap::new();
    let mut pending_refreshes = 0;
    let mut dirty = true;

    loop {
        if dirty {
            terminal.draw(&mut app)?;
            dirty = false;
        }

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
                dirty = true;
                match app.handle_event(&terminal_event) {
                    AppAction::None => {}
                    AppAction::Quit => break,
                    AppAction::Submit(objective) => {
                        let request_id = RequestId(next_request_id);
                        next_request_id += 1;
                        let idempotency_key = Uuid::new_v4().to_string();
                        let attached_session_id = app.attached_session_id().map(str::to_owned);
                        app.submission_started(idempotency_key.clone(), objective.clone());
                        let (method, params) = session_message_request(
                            attached_session_id.as_deref(),
                            &objective,
                            &idempotency_key,
                        );
                        if let Err(error) = send_request(
                            &mut input,
                            &RpcRequest::new(request_id, method, params),
                        )
                        .await
                        {
                            app.submission_failed(&idempotency_key, &error.to_string());
                            dirty = true;
                            continue;
                        }
                        let replaced = pending.insert(
                            request_id,
                            RequestPurpose::Submit(idempotency_key),
                        );
                        debug_assert!(replaced.is_none());
                    }
                }
            }
            line = output.next() => {
                let Some(line) = line else {
                    return Err(io::Error::new(
                        io::ErrorKind::UnexpectedEof,
                        "zeta RPC closed",
                    )
                    .into());
                };
                let line = line?;
                let message: IncomingMessage = serde_json::from_str(&line)?;
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
                            pending_refreshes -= 1;
                        }
                        if let Err(error) = apply_response(&mut app, &purpose, response.result) {
                            match &purpose {
                                RequestPurpose::Submit(submission_id) => {
                                    app.submission_failed(submission_id, &error.to_string());
                                }
                                RequestPurpose::RefreshSessions | RequestPurpose::RefreshEvents => {
                                    return Err(error);
                                }
                            }
                        }
                        dirty = true;
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
                        apply_failure(
                            &mut app,
                            purpose,
                            response.error.code,
                            &response.error.message,
                            response.error.data,
                        )?;
                        dirty = true;
                    }
                    IncomingMessage::Notification(notification) => {
                        validate_jsonrpc_version(&notification.jsonrpc)?;
                    }
                }
            }
            _ = refresh_interval.tick(), if pending_refreshes == 0 => {
                app.advance_animation();
                dirty = true;
                let request_id = RequestId(next_request_id);
                next_request_id += 1;
                send_request(
                    &mut input,
                    &RpcRequest::new(request_id, "session.list", json!({})),
                )
                .await?;
                let replaced = pending.insert(request_id, RequestPurpose::RefreshSessions);
                debug_assert!(replaced.is_none());

                let request_id = RequestId(next_request_id);
                next_request_id += 1;
                send_request(
                    &mut input,
                    &RpcRequest::new(
                        request_id,
                        "events.list",
                        events_list_params(app.cursor()),
                    ),
                )
                .await?;
                let replaced = pending.insert(request_id, RequestPurpose::RefreshEvents);
                debug_assert!(replaced.is_none());
                pending_refreshes = 2;
            }
        }
    }

    terminal.restore()?;
    drop(input);
    let status = child.wait().await?;
    if !status.success() {
        return Err(io::Error::other(format!("zeta RPC exited with {status}")).into());
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
        RequestPurpose, apply_failure, apply_response, events_list_params, session_message_request,
    };
    use crate::app::{App, AppAction};

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
}
