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
};

const MAX_JSONRPC_LINE_BYTES: usize = 1024 * 1024;
const REFRESH_INTERVAL: Duration = Duration::from_millis(500);

type BoxError = Box<dyn Error>;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum RequestPurpose {
    RefreshSessions,
    RefreshEvents,
    Submit,
}

impl RequestPurpose {
    fn is_refresh(self) -> bool {
        match self {
            Self::RefreshSessions | Self::RefreshEvents => true,
            Self::Submit => false,
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
                        let (method, params) = session_message_request(
                            app.attached_session_id(),
                            &objective,
                            &idempotency_key,
                        );
                        send_request(
                            &mut input,
                            &RpcRequest::new(request_id, method, params),
                        )
                        .await?;
                        let replaced = pending.insert(request_id, RequestPurpose::Submit);
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
                        apply_response(&mut app, purpose, response.result)?;
                        dirty = true;
                    }
                    IncomingMessage::Failure(response) => {
                        validate_jsonrpc_version(&response.jsonrpc)?;
                        let Some(_purpose) = pending.remove(&response.id) else {
                            return Err(io::Error::other(format!(
                                "received error for unknown request {}",
                                response.id.0
                            ))
                            .into());
                        };
                        return Err(rpc_error(
                            response.error.code,
                            &response.error.message,
                            response.error.data,
                        ));
                    }
                    IncomingMessage::Notification(notification) => {
                        validate_jsonrpc_version(&notification.jsonrpc)?;
                    }
                }
            }
            _ = refresh_interval.tick(), if pending_refreshes == 0 => {
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

fn apply_response(app: &mut App, purpose: RequestPurpose, result: Value) -> Result<(), BoxError> {
    match purpose {
        RequestPurpose::RefreshSessions => {
            let sessions: SessionsListResult = serde_json::from_value(result)?;
            app.replace_sessions(sessions.sessions);
        }
        RequestPurpose::RefreshEvents => {
            let listed: EventsListResult = serde_json::from_value(result)?;
            app.append_events(listed.events, listed.next_cursor);
        }
        RequestPurpose::Submit => {}
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::{RequestPurpose, apply_response, events_list_params, session_message_request};
    use crate::app::App;

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
            RequestPurpose::RefreshEvents,
            json!({"events": [], "next_cursor": 42}),
        )
        .expect("event refresh should apply");
        apply_response(
            &mut app,
            RequestPurpose::RefreshSessions,
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
