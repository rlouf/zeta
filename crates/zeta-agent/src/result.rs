//! Serializable outcomes from one agent invocation.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use zeta_journal::DraftEvent;

use crate::control::AgentRequest;
use crate::trace::{PromptTrace, TraceBatch};

/// Names why an ordinary run stopped.
#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RunStopReason {
    /// Stops after a model returns no tool calls.
    Finished,
    /// Stops because a control capability requested it.
    ToolStop,
    /// Stops after the configured model-call budget.
    MaxTurns,
}

/// Names one observable state-machine step.
#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum StepName {
    /// Checks cancellation, deadline, and model-call budget.
    CheckBudget,
    /// Builds and addresses one model prompt.
    BuildPrompt,
    /// Calls the injected model gateway.
    CallModel,
    /// Projects the normalized assistant response.
    RecordAssistant,
    /// Projects a requested capability call.
    RecordCapabilityCall,
    /// Calls the control operation or injected executor.
    ExecuteCapability,
    /// Projects the terminal capability result.
    RecordCapabilityResult,
    /// Finalizes an ordinary run.
    FinishRun,
    /// Finalizes a cooperative abort.
    AbortRun,
}

/// Returns proposals, traces, and telemetry from one invocation.
#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
pub struct AgentRunResult {
    /// Carries the selected final answer.
    pub final_answer: String,
    /// Identifies a selected final content object.
    pub final_object_id: Option<String>,
    /// Names why an ordinary run stopped.
    pub stop_reason: Option<RunStopReason>,
    /// Reports whether any final-answer text was streamed.
    pub answer_streamed: bool,
    /// Carries telemetry from the latest model call.
    pub telemetry: Map<String, Value>,
    /// Preserves telemetry from every model call in order.
    pub model_telemetry_calls: Vec<Map<String, Value>>,
    /// Carries durable event proposals in execution order.
    pub events: Vec<DraftEvent>,
    /// Carries ordered control proposals for the caller to commit.
    pub requests: Vec<AgentRequest>,
    /// Links every model request to its traced response.
    pub prompt_traces: Vec<PromptTrace>,
    /// Preserves the state-machine path taken by the run.
    pub steps: Vec<StepName>,
    /// Carries deterministic objects and derivations for caller persistence.
    pub trace: TraceBatch,
}
