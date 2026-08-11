//! Serializable inputs for one resolved agent invocation.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::capability::CapabilityId;

fn default_max_model_calls() -> usize {
    25
}

fn default_max_tokens() -> u64 {
    8_192
}

fn default_tool_choice() -> Value {
    Value::String("auto".to_owned())
}

/// Carries environmental values that participate in prompt identity.
///
/// # Examples
///
/// ```
/// let environment = zeta_agent::PromptEnvironment {
///     working_directory: "/workspace/zeta".to_owned(),
///     calendar_date: "2026-08-12".to_owned(),
/// };
/// assert_eq!(environment.calendar_date, "2026-08-12");
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
pub struct PromptEnvironment {
    /// Supplies the directory shown to the model.
    pub working_directory: String,
    /// Supplies the ISO calendar date shown to the model.
    pub calendar_date: String,
}

/// Contains every resolved value needed to execute one invocation.
///
/// The value is portable and carries no provider client, store, process, or
/// database connection. Runtime services are supplied separately to
/// [`AgentRunner`](crate::AgentRunner).
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(default)]
pub struct AgentInvocation {
    /// States the current objective.
    pub objective: String,
    /// Carries the prior normalized event timeline.
    pub timeline: Vec<Map<String, Value>>,
    /// Adds explicit project or caller context.
    pub context: String,
    /// Supplies the caller-owned system instruction.
    pub system_prompt: Option<String>,
    /// Grants canonical capability ids in caller order.
    pub allowed_capabilities: Vec<CapabilityId>,
    /// Bounds model requests while allowing a final pending tool batch.
    pub max_model_calls: usize,
    /// Selects the resolved provider model name.
    pub model_name: Option<String>,
    /// Selects the resolved provider endpoint.
    pub model_url: Option<String>,
    /// Selects the resolved provider protocol.
    pub model_api: Option<String>,
    /// Carries the resolved provider reasoning option.
    pub thinking: Option<String>,
    /// Associates model transport state with a session.
    pub model_session_id: Option<String>,
    /// Supplies the model-facing output budget.
    pub max_tokens: u64,
    /// Supplies the model-facing tool selection value.
    pub tool_choice: Value,
    /// Scopes tool execution to one explicit directory.
    pub base_directory: Option<String>,
    /// Stabilizes side-effect identities across retries.
    pub effect_scope: Option<String>,
    /// Enables retry-stable control handles for a queue item.
    pub source_queue_item_id: Option<String>,
    /// Associates cancellation requests with an authored agent.
    pub source_agent_id: Option<String>,
    /// Associates cancellation requests with an authored session.
    pub source_session_id: Option<String>,
    /// Names the causal parent of the first model proposal.
    pub caused_by: Option<String>,
    /// Carries prompt identity inputs that must not come from process state.
    pub environment: PromptEnvironment,
    /// Stops the run at this caller-defined clock value.
    pub deadline_ms: Option<i64>,
    /// Lists schemas the publish control operation may propose.
    pub publishable_events: Map<String, Value>,
    /// Lists schemas the return control operation may propose.
    pub returnable_events: Map<String, Value>,
}

impl Default for AgentInvocation {
    fn default() -> Self {
        AgentInvocation {
            objective: String::new(),
            timeline: Vec::new(),
            context: String::new(),
            system_prompt: None,
            allowed_capabilities: Vec::new(),
            max_model_calls: default_max_model_calls(),
            model_name: None,
            model_url: None,
            model_api: None,
            thinking: None,
            model_session_id: None,
            max_tokens: default_max_tokens(),
            tool_choice: default_tool_choice(),
            base_directory: None,
            effect_scope: None,
            source_queue_item_id: None,
            source_agent_id: None,
            source_session_id: None,
            caused_by: None,
            environment: PromptEnvironment {
                working_directory: String::new(),
                calendar_date: String::new(),
            },
            deadline_ms: None,
            publishable_events: Map::new(),
            returnable_events: Map::new(),
        }
    }
}
