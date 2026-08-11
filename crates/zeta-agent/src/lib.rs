//! Executes one resolved agent invocation through injected model and tool boundaries.

mod capability;
mod control;
mod error;
mod invocation;
mod model;
mod prompt;
mod result;
mod runner;
mod trace;

pub use capability::{
    Capability, CapabilityId, CapabilityInvocation, DeliverySemantics, EffectEvent, EffectRecorder,
    EffectStatus, IdSource, ToolExecutor, ToolFuture,
};
pub use control::AgentRequest;
pub use error::{AgentError, AgentErrorKind, AgentRunAborted, AgentRunError};
pub use invocation::{AgentInvocation, PromptEnvironment};
pub use model::{
    AbortReason, AbortSignal, AgentObserver, Clock, ModelGateway, ModelInput, ModelOutput,
    ModelRequest, Observation,
};
pub use prompt::{build_prompt, PromptBuild, PromptComponent, PromptInput};
pub use result::{AgentRunResult, RunStopReason, StepName};
pub use runner::AgentRunner;
pub use trace::{AddressedDerivation, AddressedObject, PromptTrace, TraceBatch};
