//! Caller-owned content operations used during one agent invocation.

use std::future::Future;
use std::pin::Pin;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::error::AgentError;
use crate::prompt::PromptComponent;
use crate::trace::TraceBatch;

/// Resolves one content operation without exposing its storage implementation.
pub type ContentFuture<'a> =
    Pin<Box<dyn Future<Output = Result<ContentOperation, AgentError>> + 'a>>;

/// Describes one durable content promotion proposed by a successful run.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
pub struct ContentPromotion {
    /// Selects the durable content scope.
    pub scope: String,
    /// Names the content entry within that scope.
    pub key: String,
    /// Identifies the object to make active, or removes the entry when absent.
    pub object_id: Option<String>,
    /// Protects the destination head from a concurrent move.
    pub expected_head: Option<String>,
    /// Protects the destination entry from a concurrent replacement.
    pub expected_object_id: Option<String>,
    /// Identifies the run head that produced the proposal.
    pub source_head: String,
    /// Records why the content should become durable.
    pub reason: String,
}

/// Selects a reachable content object as the invocation's final answer.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
pub struct ContentSelection {
    /// Identifies the selected graph object.
    pub object_id: String,
    /// Carries the resolved answer text.
    pub content: String,
}

/// Returns one normalized content result and its persistence proposals.
#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
pub struct ContentOperation {
    /// Carries the model-visible tool result.
    pub result: Map<String, Value>,
    /// Lists promotions to commit only after the run succeeds.
    pub promotions: Vec<ContentPromotion>,
    /// Selects the final answer when the operation completes the run.
    pub final_selection: Option<ContentSelection>,
    /// Carries newly produced graph objects and derivations.
    pub trace: TraceBatch,
}

/// Gives an invocation authorized access to caller-owned content state.
pub trait ContentService {
    /// Projects the current content head into the next model prompt.
    ///
    /// # Errors
    ///
    /// Returns [`AgentError`] when the authorized content view cannot be read.
    fn prompt_components(&mut self) -> Result<Vec<PromptComponent>, AgentError>;

    /// Queries the authorized content view.
    fn query<'a>(&'a mut self, params: &'a Map<String, Value>) -> ContentFuture<'a>;

    /// Creates a new isolated run-content revision.
    fn transform<'a>(&'a mut self, params: &'a Map<String, Value>) -> ContentFuture<'a>;

    /// Selects one reachable content object as the final answer.
    fn finish<'a>(&'a mut self, params: &'a Map<String, Value>) -> ContentFuture<'a>;
}
