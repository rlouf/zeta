//! Provider-neutral model values and injected runtime boundaries.

use std::fmt;
use std::future::Future;
use std::pin::Pin;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::error::AgentError;

mod chat_completions;
mod http;
mod responses;
mod sse;

#[cfg(test)]
mod live_tests;

#[cfg(test)]
mod vector_tests;

pub use chat_completions::{
    chat_completions_request, decode_chat_completions_stream, http_error_detail,
};
pub use http::{
    HttpModelGateway, HttpModelGatewayConfig, ModelHttpEndpoint, ModelTransportTimeouts,
};
pub use responses::{
    CodexCredentials, codex_request_headers, decode_responses_stream, responses_request,
};
pub use sse::{ModelStreamTimeout, SseByteDecoder, model_stream_timeout, parse_sse_lines};

/// Carries a model-ready request without provider transport details.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct ModelInput {
    /// Lists chat messages in their exact render order.
    pub messages: Vec<Map<String, Value>>,
    /// Lists model-facing function descriptors.
    pub tools: Vec<Value>,
    /// Carries the provider-neutral tool selection value.
    pub tool_choice: Value,
    /// Bounds completion output tokens.
    pub max_tokens: u64,
    /// Selects a model when the caller resolved one.
    pub selected_model: Option<String>,
    /// Associates provider state with a session.
    pub session_id: Option<String>,
    /// Carries the resolved reasoning option.
    pub thinking: Option<String>,
}

/// Carries provider selection metadata beside a normalized model input.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
pub struct ModelRequest {
    /// Selects a provider protocol.
    pub api: Option<String>,
    /// Selects a provider model.
    pub model: Option<String>,
    /// Selects a provider endpoint.
    pub url: Option<String>,
    /// Carries a provider reasoning option.
    pub thinking: Option<String>,
    /// Associates provider state with a session.
    pub session_id: Option<String>,
}

/// Returns one normalized assistant message and its request telemetry.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct ModelOutput {
    /// Carries assistant content, reasoning, and tool calls.
    pub message: Map<String, Value>,
    /// Carries normalized provider telemetry.
    pub telemetry: Map<String, Value>,
    /// Reports whether the provider emitted any text delta.
    #[serde(default)]
    pub streamed_content: bool,
}

/// Carries one reconstructed provider response and its transient deltas.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct DecodedModelStream {
    /// Carries the provider response after all stream fragments are joined.
    pub response: Map<String, Value>,
    /// Preserves visible deltas in arrival order.
    pub observations: Vec<Observation>,
}

/// Reports transient output without turning it into a durable event proposal.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Observation {
    /// Carries one streamed answer fragment.
    TextDelta { text: String },
    /// Carries one streamed reasoning fragment.
    ReasoningDelta { text: String },
    /// Carries a transient run status.
    Status { status: String, text: String },
}

/// Receives transient observations while a run is active.
pub trait AgentObserver {
    /// Records one transient observation.
    fn observe(&mut self, observation: Observation);
}

/// Names a cooperative reason to stop work.
#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AbortReason {
    /// Stops because the caller cancelled the run.
    Cancelled,
    /// Stops because the caller-defined deadline passed.
    DeadlineExceeded,
}

impl fmt::Display for AbortReason {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            AbortReason::Cancelled => write!(formatter, "cancelled"),
            AbortReason::DeadlineExceeded => write!(formatter, "deadline_exceeded"),
        }
    }
}

/// Supplies cooperative cancellation independently of a runtime framework.
pub trait AbortSignal {
    /// Returns the current stop reason when the run should abort.
    fn reason(&self) -> Option<AbortReason>;
}

/// Supplies time without reading a process-global clock.
pub trait Clock {
    /// Returns the caller's current time in milliseconds.
    fn now_millis(&self) -> i64;
}

/// Generates one normalized model response.
pub trait ModelGateway {
    /// Starts one provider request and emits transient observations.
    fn generate<'a>(
        &'a mut self,
        input: &'a ModelInput,
        request: &'a ModelRequest,
        observer: &'a mut dyn AgentObserver,
        abort: &'a dyn AbortSignal,
    ) -> Pin<Box<dyn Future<Output = Result<ModelOutput, AgentError>> + 'a>>;
}
