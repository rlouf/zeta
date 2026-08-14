//! Drives one configured model endpoint over bounded streaming HTTP.

use std::collections::BTreeMap;
use std::future::Future;
use std::pin::Pin;
use std::time::Duration;

use futures_util::StreamExt;
use reqwest::header::{ACCEPT, AUTHORIZATION, CONTENT_TYPE, HeaderMap, HeaderName, HeaderValue};
use serde_json::{Map, Value};

use super::chat_completions::{ChatStreamDecoder, chat_completions_request};
use super::responses::{ResponsesStreamDecoder, responses_request};
use super::{
    AbortReason, AbortSignal, AgentObserver, DecodedModelStream, ModelGateway, ModelInput,
    ModelOutput, ModelRequest, Observation, SseByteDecoder,
};
use crate::error::AgentError;

const CHAT_COMPLETIONS_API: &str = "chat-completions";
const RESPONSES_API: &str = "codex-responses";
const DEFAULT_MAX_SSE_EVENT_BYTES: usize = 1024 * 1024;
const ABORT_POLL_INTERVAL: Duration = Duration::from_millis(10);

/// Supplies one model endpoint and its caller-provided authentication data.
#[derive(Clone)]
pub struct ModelHttpEndpoint {
    url: String,
    bearer_token: Option<String>,
    headers: BTreeMap<String, String>,
}

impl ModelHttpEndpoint {
    /// Creates one endpoint without implicit credentials or headers.
    ///
    /// # Examples
    ///
    /// ```
    /// let endpoint = zeta_agent::ModelHttpEndpoint::new("http://127.0.0.1:8080/v1/chat/completions");
    /// let _gateway = zeta_agent::HttpModelGateway::new(
    ///     zeta_agent::HttpModelGatewayConfig::new(Some(endpoint), None),
    /// ).unwrap();
    /// ```
    pub fn new(url: impl Into<String>) -> Self {
        ModelHttpEndpoint {
            url: url.into(),
            bearer_token: None,
            headers: BTreeMap::new(),
        }
    }

    /// Adds a caller-provided bearer token.
    pub fn with_bearer_token(mut self, token: impl Into<String>) -> Self {
        self.bearer_token = Some(token.into());
        self
    }

    /// Adds one caller-provided request header.
    pub fn with_header(mut self, name: impl Into<String>, value: impl Into<String>) -> Self {
        self.headers.insert(name.into(), value.into());
        self
    }
}

/// Defines first-output, idle, and total request bounds.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ModelTransportTimeouts {
    /// Bounds the complete request through its first response bytes.
    pub first_output: Duration,
    /// Bounds silence between received response byte chunks.
    pub idle: Duration,
    /// Bounds the complete request including all streamed output.
    pub total: Duration,
}

impl ModelTransportTimeouts {
    /// Creates explicit transport timeout bounds.
    pub fn new(first_output: Duration, idle: Duration, total: Duration) -> Self {
        ModelTransportTimeouts {
            first_output,
            idle,
            total,
        }
    }
}

impl Default for ModelTransportTimeouts {
    fn default() -> Self {
        ModelTransportTimeouts {
            first_output: Duration::from_secs(600),
            idle: Duration::from_secs(120),
            total: Duration::from_secs(1800),
        }
    }
}

/// Configures the concrete streamed HTTP gateway.
#[derive(Clone)]
pub struct HttpModelGatewayConfig {
    chat_completions: Option<ModelHttpEndpoint>,
    responses: Option<ModelHttpEndpoint>,
    timeouts: ModelTransportTimeouts,
    max_sse_event_bytes: usize,
}

impl HttpModelGatewayConfig {
    /// Creates a configuration with explicit protocol endpoints.
    pub fn new(
        chat_completions: Option<ModelHttpEndpoint>,
        responses: Option<ModelHttpEndpoint>,
    ) -> Self {
        HttpModelGatewayConfig {
            chat_completions,
            responses,
            timeouts: ModelTransportTimeouts::default(),
            max_sse_event_bytes: DEFAULT_MAX_SSE_EVENT_BYTES,
        }
    }

    /// Replaces all three timeout bounds.
    pub fn with_timeouts(mut self, timeouts: ModelTransportTimeouts) -> Self {
        self.timeouts = timeouts;
        self
    }

    /// Replaces the maximum buffered bytes for one SSE event.
    pub fn with_max_sse_event_bytes(mut self, max_sse_event_bytes: usize) -> Self {
        self.max_sse_event_bytes = max_sse_event_bytes;
        self
    }
}

/// Streams live model responses through the provider-neutral gateway boundary.
pub struct HttpModelGateway {
    client: reqwest::Client,
    config: HttpModelGatewayConfig,
}

impl HttpModelGateway {
    /// Creates a gateway without reading credentials or endpoint configuration.
    ///
    /// # Errors
    ///
    /// Returns [`AgentError`] when an injected header cannot be represented by
    /// the HTTP client.
    pub fn new(config: HttpModelGatewayConfig) -> Result<Self, AgentError> {
        validate_endpoint(config.chat_completions.as_ref())?;
        validate_endpoint(config.responses.as_ref())?;
        let client = reqwest::Client::builder()
            .connect_timeout(config.timeouts.first_output)
            .build()
            .map_err(|error| AgentError::model(format!("model client failed: {error}")))?;
        Ok(HttpModelGateway { client, config })
    }
}

impl ModelGateway for HttpModelGateway {
    fn generate<'a>(
        &'a mut self,
        input: &'a ModelInput,
        request: &'a ModelRequest,
        observer: &'a mut dyn AgentObserver,
        abort: &'a dyn AbortSignal,
    ) -> Pin<Box<dyn Future<Output = Result<ModelOutput, AgentError>> + 'a>> {
        Box::pin(async move {
            if let Some(reason) = abort.reason() {
                return Err(abort_error(reason));
            }
            let protocol = Protocol::parse(request.api.as_deref())?;
            let endpoint = self.endpoint(protocol, request.url.as_deref())?;
            let input = resolved_input(input, request);
            let body = match protocol {
                Protocol::ChatCompletions => chat_completions_request(&input)?,
                Protocol::Responses => responses_request(&input)?,
            };
            let future = self.stream(protocol, endpoint, body, observer, abort);
            match tokio::time::timeout(self.config.timeouts.total, future).await {
                Ok(result) => result,
                Err(_) => Err(AgentError::model("model request exceeded total timeout")),
            }
        })
    }
}

impl HttpModelGateway {
    async fn stream(
        &self,
        protocol: Protocol,
        endpoint: ModelHttpEndpoint,
        mut body: Map<String, Value>,
        observer: &mut dyn AgentObserver,
        abort: &dyn AbortSignal,
    ) -> Result<ModelOutput, AgentError> {
        body.insert("stream".to_owned(), Value::Bool(true));
        let headers = endpoint_headers(&endpoint)?;
        let request = self.client.post(&endpoint.url).headers(headers).json(&body);
        let first_output_started = tokio::time::Instant::now();
        let response = wait_for(request.send(), self.config.timeouts.first_output, abort)
            .await
            .map_err(|failure| failure.into_error(WaitPhase::FirstOutput))?
            .map_err(|error| AgentError::model(format!("model request failed: {error}")))?;
        let status = response.status();
        if status.is_client_error() || status.is_server_error() || status.is_redirection() {
            let url = response.url().to_string();
            let body = wait_for(response.bytes(), self.config.timeouts.idle, abort)
                .await
                .map_err(|failure| failure.into_error(WaitPhase::Idle))?
                .map_err(|error| AgentError::model(format!("model request failed: {error}")))?;
            let body = if body.len() > 2048 {
                &body[..2048]
            } else {
                &body
            };
            let body = serde_json::from_slice::<Value>(body)
                .unwrap_or_else(|_| Value::String(String::from_utf8_lossy(body).trim().to_owned()));
            return Err(AgentError::model(super::http_error_detail(
                status.as_u16(),
                &url,
                &body,
            )));
        }
        let mut stream = response.bytes_stream();
        let mut sse = SseByteDecoder::new(self.config.max_sse_event_bytes);
        let mut decoder = ProtocolDecoder::new(protocol);
        let mut received_output = false;
        loop {
            let (timeout, phase) = if received_output {
                (self.config.timeouts.idle, WaitPhase::Idle)
            } else {
                let timeout = self
                    .config
                    .timeouts
                    .first_output
                    .checked_sub(first_output_started.elapsed())
                    .filter(|timeout| !timeout.is_zero())
                    .ok_or_else(|| {
                        AgentError::model("model request timed out before first output")
                    })?;
                (timeout, WaitPhase::FirstOutput)
            };
            let next = wait_for(stream.next(), timeout, abort)
                .await
                .map_err(|failure| failure.into_error(phase))?;
            let Some(next) = next else {
                break;
            };
            received_output = true;
            let bytes =
                next.map_err(|error| AgentError::model(format!("model request failed: {error}")))?;
            for frame in sse.push(&bytes)? {
                forward_observations(decoder.push_frame(&frame)?, observer);
            }
        }
        for frame in sse.finish()? {
            forward_observations(decoder.push_frame(&frame)?, observer);
        }
        if let Some(reason) = abort.reason() {
            return Err(abort_error(reason));
        }
        let decoded = decoder.finish()?;
        Ok(model_output(decoded))
    }

    fn endpoint(
        &self,
        protocol: Protocol,
        selected_url: Option<&str>,
    ) -> Result<ModelHttpEndpoint, AgentError> {
        let endpoint = match protocol {
            Protocol::ChatCompletions => self.config.chat_completions.as_ref(),
            Protocol::Responses => self.config.responses.as_ref(),
        };
        let Some(endpoint) = endpoint else {
            return Err(AgentError::model(format!(
                "model request failed: no {} endpoint is configured",
                protocol.as_str()
            )));
        };
        let mut endpoint = endpoint.clone();
        if let Some(selected_url) = selected_url {
            endpoint.url = selected_url.to_owned();
        }
        Ok(endpoint)
    }
}

#[derive(Clone, Copy)]
enum Protocol {
    ChatCompletions,
    Responses,
}

impl Protocol {
    fn parse(value: Option<&str>) -> Result<Self, AgentError> {
        match value.unwrap_or(CHAT_COMPLETIONS_API) {
            CHAT_COMPLETIONS_API => Ok(Protocol::ChatCompletions),
            RESPONSES_API => Ok(Protocol::Responses),
            value => Err(AgentError::model(format!(
                "model request failed: unsupported model API {value}"
            ))),
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Protocol::ChatCompletions => CHAT_COMPLETIONS_API,
            Protocol::Responses => RESPONSES_API,
        }
    }
}

enum ProtocolDecoder {
    ChatCompletions(ChatStreamDecoder),
    Responses(ResponsesStreamDecoder),
}

impl ProtocolDecoder {
    fn new(protocol: Protocol) -> Self {
        match protocol {
            Protocol::ChatCompletions => {
                ProtocolDecoder::ChatCompletions(ChatStreamDecoder::default())
            }
            Protocol::Responses => ProtocolDecoder::Responses(ResponsesStreamDecoder::default()),
        }
    }

    fn push_frame(&mut self, frame: &str) -> Result<Vec<Observation>, AgentError> {
        match self {
            ProtocolDecoder::ChatCompletions(decoder) => decoder.push_frame(frame),
            ProtocolDecoder::Responses(decoder) => decoder.push_frame(frame),
        }
    }

    fn finish(self) -> Result<DecodedModelStream, AgentError> {
        match self {
            ProtocolDecoder::ChatCompletions(decoder) => decoder.finish(),
            ProtocolDecoder::Responses(decoder) => decoder.finish(),
        }
    }
}

enum WaitFailure {
    Aborted(AbortReason),
    TimedOut,
}

#[derive(Clone, Copy)]
enum WaitPhase {
    FirstOutput,
    Idle,
}

impl WaitFailure {
    fn into_error(self, phase: WaitPhase) -> AgentError {
        match self {
            WaitFailure::Aborted(reason) => abort_error(reason),
            WaitFailure::TimedOut => match phase {
                WaitPhase::FirstOutput => {
                    AgentError::model("model request timed out before first output")
                }
                WaitPhase::Idle => {
                    AgentError::model("model request timed out waiting for streamed output")
                }
            },
        }
    }
}

async fn wait_for<F, T>(
    future: F,
    timeout: Duration,
    abort: &dyn AbortSignal,
) -> Result<T, WaitFailure>
where
    F: Future<Output = T>,
{
    let mut future = Box::pin(future);
    let deadline = tokio::time::sleep(timeout);
    let mut deadline = Box::pin(deadline);
    loop {
        if let Some(reason) = abort.reason() {
            return Err(WaitFailure::Aborted(reason));
        }
        tokio::select! {
            output = &mut future => return Ok(output),
            () = &mut deadline => return Err(WaitFailure::TimedOut),
            () = tokio::time::sleep(ABORT_POLL_INTERVAL) => {}
        }
    }
}

fn validate_endpoint(endpoint: Option<&ModelHttpEndpoint>) -> Result<(), AgentError> {
    let Some(endpoint) = endpoint else {
        return Ok(());
    };
    endpoint_headers(endpoint).map(|_| ())
}

fn endpoint_headers(endpoint: &ModelHttpEndpoint) -> Result<HeaderMap, AgentError> {
    let mut headers = HeaderMap::new();
    headers.insert(ACCEPT, HeaderValue::from_static("text/event-stream"));
    headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    if let Some(token) = &endpoint.bearer_token {
        let value = HeaderValue::from_str(&format!("Bearer {token}"))
            .map_err(|_| AgentError::model("model client failed: invalid bearer token"))?;
        headers.insert(AUTHORIZATION, value);
    }
    for (name, value) in &endpoint.headers {
        let name = HeaderName::from_bytes(name.as_bytes()).map_err(|_| {
            AgentError::model(format!("model client failed: invalid header {name}"))
        })?;
        let value = HeaderValue::from_str(value).map_err(|_| {
            AgentError::model(format!(
                "model client failed: invalid header value for {name}"
            ))
        })?;
        headers.insert(name, value);
    }
    Ok(headers)
}

fn resolved_input(input: &ModelInput, request: &ModelRequest) -> ModelInput {
    let mut input = input.clone();
    if request.model.is_some() {
        input.selected_model = request.model.clone();
    }
    if request.session_id.is_some() {
        input.session_id = request.session_id.clone();
    }
    if request.thinking.is_some() {
        input.thinking = request.thinking.clone();
    }
    input
}

fn forward_observations(observations: Vec<Observation>, observer: &mut dyn AgentObserver) {
    for observation in observations {
        observer.observe(observation);
    }
}

fn model_output(decoded: DecodedModelStream) -> ModelOutput {
    let choices = decoded.response.get("choices").and_then(Value::as_array);
    let message = choices
        .and_then(|choices| choices.first())
        .and_then(Value::as_object)
        .and_then(|choice| choice.get("message"))
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let mut telemetry = Map::new();
    if let Some(usage) = decoded.response.get("usage") {
        telemetry.insert("usage".to_owned(), usage.clone());
    }
    let mut streamed_content = false;
    for observation in decoded.observations {
        match observation {
            Observation::TextDelta { text: _ } => streamed_content = true,
            Observation::ReasoningDelta { text: _ } => {}
            Observation::Status { status: _, text: _ } => {}
        }
    }
    ModelOutput {
        message,
        telemetry,
        streamed_content,
    }
}

fn abort_error(reason: AbortReason) -> AgentError {
    AgentError::model(format!("model request aborted: {reason}"))
}
