//! Errors returned while preparing or executing one invocation.

use std::fmt;

use serde::{Deserialize, Serialize};

use crate::result::AgentRunResult;

/// Classifies a failure at the provider-neutral agent boundary.
#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AgentErrorKind {
    /// Rejects an invalid resolved invocation.
    Invocation,
    /// Rejects prompt data that cannot be rendered or addressed.
    Prompt,
    /// Reports a model-boundary failure.
    Model,
    /// Reports a capability-boundary failure.
    Tool,
    /// Reports an unavailable or invalid generated identity.
    Identity,
    /// Reports a trace value that cannot be encoded.
    Trace,
    /// Reports a failed immediate durable-draft recording.
    Durability,
}

/// Describes one non-abort agent failure.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
pub struct AgentError {
    /// Classifies the failing boundary.
    pub kind: AgentErrorKind,
    /// Explains the failure for a human reviewer.
    pub message: String,
}

impl AgentError {
    /// Creates an invocation validation error.
    pub fn invocation(message: impl Into<String>) -> Self {
        Self::new(AgentErrorKind::Invocation, message)
    }

    /// Creates a prompt construction error.
    pub fn prompt(message: impl Into<String>) -> Self {
        Self::new(AgentErrorKind::Prompt, message)
    }

    /// Creates a model-boundary error.
    pub fn model(message: impl Into<String>) -> Self {
        Self::new(AgentErrorKind::Model, message)
    }

    /// Creates a tool-boundary error.
    pub fn tool(message: impl Into<String>) -> Self {
        Self::new(AgentErrorKind::Tool, message)
    }

    /// Creates an identity-source error.
    pub fn identity(message: impl Into<String>) -> Self {
        Self::new(AgentErrorKind::Identity, message)
    }

    /// Creates a trace construction error.
    pub fn trace(message: impl Into<String>) -> Self {
        Self::new(AgentErrorKind::Trace, message)
    }

    /// Creates a durable-draft recording error.
    pub fn durability(message: impl Into<String>) -> Self {
        Self::new(AgentErrorKind::Durability, message)
    }

    fn new(kind: AgentErrorKind, message: impl Into<String>) -> Self {
        AgentError {
            kind,
            message: message.into(),
        }
    }
}

impl fmt::Display for AgentError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}", self.message)
    }
}

impl std::error::Error for AgentError {}

/// Carries a cooperative abort reason and the partial run result.
#[derive(Clone, Debug, PartialEq)]
pub struct AgentRunAborted {
    /// Names why the run stopped.
    pub reason: crate::model::AbortReason,
    /// Preserves all proposals and traces produced before the abort.
    pub result: AgentRunResult,
}

/// Separates cooperative aborts from ordinary execution failures.
#[derive(Clone, Debug, PartialEq)]
pub enum AgentRunError {
    /// Returns the partial result of a cooperative abort.
    Aborted(Box<AgentRunAborted>),
    /// Reports a failure that has no valid run result.
    Failed(AgentError),
}

impl fmt::Display for AgentRunError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            AgentRunError::Aborted(aborted) => {
                write!(formatter, "agent run aborted: {}", aborted.reason)
            }
            AgentRunError::Failed(error) => error.fmt(formatter),
        }
    }
}

impl std::error::Error for AgentRunError {}

impl From<AgentError> for AgentRunError {
    fn from(error: AgentError) -> Self {
        AgentRunError::Failed(error)
    }
}
