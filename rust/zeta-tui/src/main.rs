mod app;
mod wire;

use std::env;
use std::error::Error;
use std::io;
use std::process::Stdio;

use futures_util::StreamExt;
use serde_json::{Value, json};
use tokio::io::AsyncWriteExt;
use tokio::process::{ChildStdin, ChildStdout, Command};
use tokio_util::codec::{FramedRead, LinesCodec};

use crate::app::{App, AppAction, run_terminal};
use crate::wire::{EventsListResult, IncomingMessage, InitializeResult, RequestId, RpcRequest};

const MAX_JSONRPC_LINE_BYTES: usize = 1024 * 1024;

type BoxError = Box<dyn Error>;

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
        &RpcRequest::new(list_id, "events.list", json!({"limit": 200})),
    )
    .await?;
    let result = receive_response(&mut output, list_id).await?;
    let listed: EventsListResult = serde_json::from_value(result)?;
    let mut app = App::connected(initialized.protocol, listed.events, listed.next_cursor);
    let mut next_request_id = 3;
    let mut next_message_id = 1;

    loop {
        match run_terminal(&mut app)? {
            AppAction::None => {}
            AppAction::Quit => break,
            AppAction::Submit(objective) => {
                let run_id = RequestId(next_request_id);
                next_request_id += 1;
                let idempotency_key = format!("zeta-tui-message-{next_message_id}");
                next_message_id += 1;
                send_request(
                    &mut input,
                    &RpcRequest::new(
                        run_id,
                        "session.run",
                        json!({
                            "objective": objective,
                            "tools": [],
                            "idempotency_key": idempotency_key
                        }),
                    ),
                )
                .await?;
                receive_response(&mut output, run_id).await?;

                let list_id = RequestId(next_request_id);
                next_request_id += 1;
                let mut params = json!({"limit": 200});
                if let Some(cursor) = app.cursor() {
                    params["after_cursor"] = json!(cursor);
                }
                send_request(&mut input, &RpcRequest::new(list_id, "events.list", params)).await?;
                let result = receive_response(&mut output, list_id).await?;
                let listed: EventsListResult = serde_json::from_value(result)?;
                app.append_events(listed.events, listed.next_cursor);
            }
        }
    }

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
                let data = match response.error.data {
                    Some(data) => format!(" ({data})"),
                    None => String::new(),
                };
                return Err(io::Error::other(format!(
                    "JSON-RPC error {}: {}{}",
                    response.error.code, response.error.message, data
                ))
                .into());
            }
            IncomingMessage::Notification(notification) => {
                validate_jsonrpc_version(&notification.jsonrpc)?;
                eprintln!(
                    "notification {} {}",
                    notification.method, notification.params
                );
            }
        }
    }
    Err(io::Error::new(io::ErrorKind::UnexpectedEof, "zeta RPC closed").into())
}

fn validate_jsonrpc_version(version: &str) -> Result<(), BoxError> {
    if version != "2.0" {
        return Err(io::Error::other(format!("unsupported JSON-RPC version {version}")).into());
    }
    Ok(())
}
