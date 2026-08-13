//! Executes one resolved agent invocation through injected model and tool boundaries.

mod capability;
mod content;
mod control;
mod error;
mod history;
mod invocation;
mod model;
mod prompt;
mod result;
mod runner;
mod tools;
mod trace;

pub use capability::{
    resolve_capabilities, ArgumentAdapter, Capability, CapabilityExecutor, CapabilityFuture,
    CapabilityId, CapabilityInvocation, DeliverySemantics, DraftRecorder, IdSource,
    ResolvedCapability, ToolProfile,
};
pub use content::{
    ContentFuture, ContentOperation, ContentPromotion, ContentSelection, ContentService,
};
pub use control::AgentProposal;
pub use error::{AgentError, AgentErrorKind, AgentRunAborted, AgentRunError};
pub use history::{HistoryFuture, HistoryService};
pub use invocation::{AgentInvocation, PromptEnvironment};
pub use model::{
    chat_completions_request, codex_request_headers, decode_chat_completions_stream,
    decode_responses_stream, http_error_detail, model_stream_timeout, parse_sse_lines,
    responses_request, AbortReason, AbortSignal, AgentObserver, Clock, CodexCredentials,
    DecodedModelStream, HttpModelGateway, HttpModelGatewayConfig, ModelGateway, ModelHttpEndpoint,
    ModelInput, ModelOutput, ModelRequest, ModelStreamTimeout, ModelTransportTimeouts, Observation,
    SseByteDecoder,
};
pub use prompt::{build_prompt, PromptBuild, PromptComponent, PromptInput, PromptTransform};
pub use result::{AgentRunResult, RunStopReason, StepName};
pub use runner::AgentRunner;
pub use tools::{
    bounded_output, native_capabilities, CommandOutput, CommandRunner, HttpFuture, HttpResponse,
    HttpTransport, NativeToolExecutor, SystemCommandRunner, UnavailableHttpTransport,
    UnavailableWebSearch, WebSearchFuture, WebSearchProvider, WebSearchResult, WebSearchSource,
};
pub use trace::{AddressedDerivation, AddressedObject, PromptTrace, TraceBatch};
