use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use serde_json::{json, Map, Value};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::task::JoinHandle;

use super::{
    AbortReason, AbortSignal, AgentObserver, HttpModelGateway, HttpModelGatewayConfig,
    ModelGateway, ModelHttpEndpoint, ModelInput, ModelRequest, ModelTransportTimeouts, Observation,
    SseByteDecoder,
};

#[derive(Default)]
struct RecordingObserver {
    observations: Vec<Observation>,
}

impl AgentObserver for RecordingObserver {
    fn observe(&mut self, observation: Observation) {
        self.observations.push(observation);
    }
}

#[derive(Default)]
struct AtomicAbort {
    cancelled: AtomicBool,
}

impl AbortSignal for AtomicAbort {
    fn reason(&self) -> Option<AbortReason> {
        if self.cancelled.load(Ordering::SeqCst) {
            return Some(AbortReason::Cancelled);
        }
        None
    }
}

struct ResponseChunk {
    delay: Duration,
    bytes: Vec<u8>,
}

async fn fake_server(chunks: Vec<ResponseChunk>) -> (String, JoinHandle<Vec<u8>>) {
    fake_server_with_timing("200 OK", Duration::ZERO, chunks).await
}

async fn fake_server_with_status(
    status: &str,
    chunks: Vec<ResponseChunk>,
) -> (String, JoinHandle<Vec<u8>>) {
    fake_server_with_timing(status, Duration::ZERO, chunks).await
}

async fn fake_server_with_timing(
    status: &str,
    header_delay: Duration,
    chunks: Vec<ResponseChunk>,
) -> (String, JoinHandle<Vec<u8>>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let status = status.to_owned();
    let task = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let request = read_request(&mut socket).await;
        tokio::time::sleep(header_delay).await;
        let headers = format!(
            "HTTP/1.1 {status}\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
        );
        socket.write_all(headers.as_bytes()).await.unwrap();
        for chunk in chunks {
            tokio::time::sleep(chunk.delay).await;
            if chunk.bytes.starts_with(b": ready") {
                let _result = socket.write_all(b"1\r\n:\r\n").await;
                let _result = socket.flush().await;
                tokio::time::sleep(Duration::from_millis(40)).await;
                continue;
            }
            let header = format!("{:X}\r\n", chunk.bytes.len());
            if socket.write_all(header.as_bytes()).await.is_err() {
                return request;
            }
            if socket.write_all(&chunk.bytes).await.is_err() {
                return request;
            }
            if socket.write_all(b"\r\n").await.is_err() {
                return request;
            }
        }
        let _result = socket.write_all(b"0\r\n\r\n").await;
        request
    });
    (format!("http://{address}/model"), task)
}

async fn read_request(socket: &mut tokio::net::TcpStream) -> Vec<u8> {
    let mut request = Vec::new();
    let mut buffer = [0_u8; 1024];
    loop {
        let count = socket.read(&mut buffer).await.unwrap();
        if count == 0 {
            return request;
        }
        request.extend_from_slice(&buffer[..count]);
        let Some(headers_end) = find_bytes(&request, b"\r\n\r\n") else {
            continue;
        };
        let headers = String::from_utf8_lossy(&request[..headers_end]);
        let mut content_length = 0;
        for line in headers.lines() {
            let Some(value) = line
                .to_ascii_lowercase()
                .strip_prefix("content-length: ")
                .map(str::to_owned)
            else {
                continue;
            };
            content_length = value.parse().unwrap();
        }
        if request.len() >= headers_end + 4 + content_length {
            return request;
        }
    }
}

fn find_bytes(bytes: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.is_empty() || needle.len() > bytes.len() {
        return None;
    }
    bytes
        .windows(needle.len())
        .position(|window| window == needle)
}

fn input() -> ModelInput {
    ModelInput {
        messages: vec![json!({"role": "user", "content": "Hello"})
            .as_object()
            .unwrap()
            .clone()],
        tools: Vec::new(),
        tool_choice: json!("auto"),
        max_tokens: 64,
        selected_model: Some("unit-model".to_owned()),
        session_id: Some("session-vector".to_owned()),
        thinking: None,
    }
}

fn request(api: &str) -> ModelRequest {
    ModelRequest {
        api: Some(api.to_owned()),
        model: Some("unit-model".to_owned()),
        url: None,
        thinking: None,
        session_id: Some("session-vector".to_owned()),
    }
}

fn config(api: &str, url: String, timeouts: ModelTransportTimeouts) -> HttpModelGatewayConfig {
    let endpoint = ModelHttpEndpoint::new(url)
        .with_bearer_token("secret-token")
        .with_header("x-vector", "present");
    if api == "codex-responses" {
        return HttpModelGatewayConfig::new(None, Some(endpoint)).with_timeouts(timeouts);
    }
    HttpModelGatewayConfig::new(Some(endpoint), None).with_timeouts(timeouts)
}

fn generous_timeouts() -> ModelTransportTimeouts {
    ModelTransportTimeouts::new(
        Duration::from_secs(1),
        Duration::from_secs(1),
        Duration::from_secs(2),
    )
}

#[tokio::test]
async fn http_gateway_streams_byte_fragmented_chat_completion() {
    let event = concat!(
        "data: {\"id\":\"chat-live\",\"choices\":[{\"index\":0,",
        "\"delta\":{\"role\":\"assistant\",\"content\":\"hé\"},",
        "\"finish_reason\":\"stop\"}]}\n\n",
        "data: [DONE]\n\n"
    )
    .as_bytes();
    let split = event.iter().position(|byte| *byte == 0xc3).unwrap() + 1;
    let chunks = vec![
        ResponseChunk {
            delay: Duration::ZERO,
            bytes: event[..7].to_vec(),
        },
        ResponseChunk {
            delay: Duration::ZERO,
            bytes: event[7..split].to_vec(),
        },
        ResponseChunk {
            delay: Duration::ZERO,
            bytes: event[split..].to_vec(),
        },
    ];
    let (url, server) = fake_server(chunks).await;
    let mut gateway =
        HttpModelGateway::new(config("chat-completions", url, generous_timeouts())).unwrap();
    let mut observer = RecordingObserver::default();

    let output = gateway
        .generate(
            &input(),
            &request("chat-completions"),
            &mut observer,
            &AtomicAbort::default(),
        )
        .await
        .unwrap();

    assert_eq!(output.message["content"], "hé");
    assert!(output.streamed_content);
    assert_eq!(
        observer.observations,
        [Observation::TextDelta {
            text: "hé".to_owned()
        }]
    );
    let request = String::from_utf8(server.await.unwrap()).unwrap();
    assert!(request.starts_with("POST /model HTTP/1.1\r\n"));
    assert!(request
        .to_ascii_lowercase()
        .contains("authorization: bearer secret-token"));
    assert!(request.to_ascii_lowercase().contains("x-vector: present"));
    let body = request.split("\r\n\r\n").nth(1).unwrap();
    let body: Value = serde_json::from_str(body).unwrap();
    assert_eq!(body["stream"], true);
}

#[tokio::test]
async fn http_gateway_selects_responses_and_normalizes_usage() {
    let chunks = vec![ResponseChunk {
        delay: Duration::ZERO,
        bytes: concat!(
            "data: {\"type\":\"response.output_text.delta\",\"delta\":\"draft\"}\n\n",
            "data: {\"type\":\"response.output_item.done\",\"item\":{\"type\":\"message\",\"content\":[{\"type\":\"output_text\",\"text\":\"done\"}]}}\n\n",
            "data: {\"type\":\"response.completed\",\"response\":{\"status\":\"completed\",\"usage\":{\"input_tokens\":3,\"output_tokens\":2,\"total_tokens\":5}}}\n\n",
            "data: [DONE]\n\n"
        )
        .as_bytes()
        .to_vec(),
    }];
    let (url, server) = fake_server(chunks).await;
    let mut gateway =
        HttpModelGateway::new(config("codex-responses", url, generous_timeouts())).unwrap();
    let mut observer = RecordingObserver::default();

    let output = gateway
        .generate(
            &input(),
            &request("codex-responses"),
            &mut observer,
            &AtomicAbort::default(),
        )
        .await
        .unwrap();

    assert_eq!(output.message["content"], "done");
    assert_eq!(
        output.telemetry,
        Map::from_iter([(
            "usage".to_owned(),
            json!({"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}),
        )])
    );
    assert_eq!(
        observer.observations,
        [Observation::TextDelta {
            text: "draft".to_owned()
        }]
    );
    let request = String::from_utf8(server.await.unwrap()).unwrap();
    let body = request.split("\r\n\r\n").nth(1).unwrap();
    let body: Value = serde_json::from_str(body).unwrap();
    assert_eq!(body["stream"], true);
    assert_eq!(body["store"], false);
}

#[tokio::test]
async fn http_gateway_reports_first_output_idle_and_total_timeouts() {
    let cases = [
        (
            vec![ResponseChunk {
                delay: Duration::from_millis(40),
                bytes: b"data: [DONE]\n\n".to_vec(),
            }],
            ModelTransportTimeouts::new(
                Duration::from_millis(10),
                Duration::from_secs(1),
                Duration::from_secs(1),
            ),
            "model request timed out before first output",
        ),
        (
            vec![
                ResponseChunk {
                    delay: Duration::ZERO,
                    bytes: b": ready".to_vec(),
                },
                ResponseChunk {
                    delay: Duration::ZERO,
                    bytes: b"data: [DONE]\n\n".to_vec(),
                },
            ],
            ModelTransportTimeouts::new(
                Duration::from_secs(1),
                Duration::from_millis(10),
                Duration::from_secs(1),
            ),
            "model request timed out waiting for streamed output",
        ),
        (
            (0..20)
                .map(|_| ResponseChunk {
                    delay: Duration::from_millis(4),
                    bytes: b": keepalive\n\n".to_vec(),
                })
                .collect(),
            ModelTransportTimeouts::new(
                Duration::from_secs(1),
                Duration::from_millis(20),
                Duration::from_millis(15),
            ),
            "model request exceeded total timeout",
        ),
    ];
    for (chunks, timeouts, expected) in cases {
        let (url, server) = fake_server(chunks).await;
        let mut gateway = HttpModelGateway::new(config("chat-completions", url, timeouts)).unwrap();
        let error = gateway
            .generate(
                &input(),
                &request("chat-completions"),
                &mut RecordingObserver::default(),
                &AtomicAbort::default(),
            )
            .await
            .unwrap_err();
        assert_eq!(error.to_string(), expected);
        server.abort();
    }
}

#[tokio::test]
async fn http_gateway_first_output_timeout_includes_headers_and_first_body_bytes() {
    let chunks = vec![ResponseChunk {
        delay: Duration::from_millis(20),
        bytes: concat!(
            "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"done\"},\"finish_reason\":\"stop\"}]}\n\n",
            "data: [DONE]\n\n"
        )
        .as_bytes()
        .to_vec(),
    }];
    let (url, server) = fake_server_with_timing("200 OK", Duration::from_millis(20), chunks).await;
    let timeouts = ModelTransportTimeouts::new(
        Duration::from_millis(30),
        Duration::from_secs(1),
        Duration::from_secs(1),
    );
    let mut gateway = HttpModelGateway::new(config("chat-completions", url, timeouts)).unwrap();

    let error = gateway
        .generate(
            &input(),
            &request("chat-completions"),
            &mut RecordingObserver::default(),
            &AtomicAbort::default(),
        )
        .await
        .unwrap_err();

    assert_eq!(
        error.to_string(),
        "model request timed out before first output"
    );
    server.abort();
}

#[tokio::test]
async fn http_gateway_total_timeout_bounds_a_streaming_error_body() {
    let mut chunks = Vec::new();
    for _index in 0..20 {
        chunks.push(ResponseChunk {
            delay: Duration::from_millis(4),
            bytes: b"x".to_vec(),
        });
    }
    let (url, server) = fake_server_with_status("429 Too Many Requests", chunks).await;
    let timeouts = ModelTransportTimeouts::new(
        Duration::from_secs(1),
        Duration::from_millis(20),
        Duration::from_millis(15),
    );
    let mut gateway = HttpModelGateway::new(config("chat-completions", url, timeouts)).unwrap();

    let error = gateway
        .generate(
            &input(),
            &request("chat-completions"),
            &mut RecordingObserver::default(),
            &AtomicAbort::default(),
        )
        .await
        .unwrap_err();

    assert_eq!(error.to_string(), "model request exceeded total timeout");
    server.abort();
}

#[tokio::test]
async fn http_gateway_checks_cancellation_while_waiting_for_output() {
    let chunks = vec![ResponseChunk {
        delay: Duration::from_secs(1),
        bytes: b"data: [DONE]\n\n".to_vec(),
    }];
    let (url, server) = fake_server(chunks).await;
    let mut gateway =
        HttpModelGateway::new(config("chat-completions", url, generous_timeouts())).unwrap();
    let abort = Arc::new(AtomicAbort::default());
    let cancel = Arc::clone(&abort);
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(10)).await;
        cancel.cancelled.store(true, Ordering::SeqCst);
    });

    let error = gateway
        .generate(
            &input(),
            &request("chat-completions"),
            &mut RecordingObserver::default(),
            abort.as_ref(),
        )
        .await
        .unwrap_err();

    assert_eq!(error.to_string(), "model request aborted: cancelled");
    server.abort();
}

#[test]
fn sse_byte_decoder_bounds_partial_events_and_preserves_split_utf8() {
    let mut decoder = SseByteDecoder::new(64);
    assert!(decoder.push(b"data: h\xc3").unwrap().is_empty());
    assert_eq!(decoder.push(b"\xa9\n\n").unwrap(), ["hé"]);

    let endpoint = ModelHttpEndpoint::new("http://127.0.0.1:1/model");
    let _config = HttpModelGatewayConfig::new(Some(endpoint), None).with_max_sse_event_bytes(8);
    let mut decoder = SseByteDecoder::new(8);
    let error = decoder.push(b"data: too much").unwrap_err();
    assert_eq!(
        error.to_string(),
        "model stream failed: SSE event exceeded 8 bytes"
    );
}
