//! Authorized history access used during one agent invocation.

use std::future::Future;
use std::pin::Pin;

use serde_json::{Map, Value};

use crate::error::AgentError;

/// Resolves one history query without exposing its persistence implementation.
pub type HistoryFuture<'a> =
    Pin<Box<dyn Future<Output = Result<Map<String, Value>, AgentError>> + 'a>>;

/// Gives an invocation read-only access to its authorized history.
pub trait HistoryService {
    /// Queries prior runs inside the caller-selected authorization boundary.
    fn query<'a>(&'a mut self, params: &'a Map<String, Value>) -> HistoryFuture<'a>;
}
