//! Ordered control proposals returned to the invocation owner.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

/// Carries one ordered proposal for the invocation owner to commit.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum AgentProposal {
    /// Proposes publishing a durable event.
    Publish {
        /// Identifies the retry-stable proposal.
        handle: String,
        /// Names the event vocabulary entry.
        event_type: String,
        /// Carries the event payload.
        payload: Map<String, Value>,
        /// Schedules publication at an optional UTC time.
        at: Option<String>,
        /// Preserves global tool-call order.
        position: usize,
    },
    /// Proposes suspending until a matching event arrives.
    Wait {
        /// Identifies the retry-stable wait.
        handle: String,
        /// Names the event vocabulary entry to match.
        event_type: String,
        /// Narrows the match to exact payload fields.
        fields: Map<String, Value>,
        /// Stops waiting at an optional UTC time.
        deadline: Option<String>,
        /// Preserves global tool-call order.
        position: usize,
    },
    /// Proposes cancelling an existing wait or deferred publication.
    Cancel {
        /// Identifies the proposal to cancel.
        handle: String,
        /// Explains the cancellation when supplied.
        reason: Option<String>,
        /// Associates the proposal with an authored agent.
        source_agent_id: String,
        /// Associates the proposal with an authored session.
        source_session_id: String,
        /// Preserves global tool-call order.
        position: usize,
    },
}
