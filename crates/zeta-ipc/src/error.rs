//! JSON-RPC errors and protocol failures.

use std::fmt;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

/// The JSON-RPC parse-error code.
pub const PARSE_ERROR: i64 = -32700;
/// The JSON-RPC invalid-request code.
pub const INVALID_REQUEST: i64 = -32600;
/// The JSON-RPC method-not-found code.
pub const METHOD_NOT_FOUND: i64 = -32601;
/// The JSON-RPC invalid-params code.
pub const INVALID_PARAMS: i64 = -32602;
/// The JSON-RPC internal-error code.
pub const INTERNAL_ERROR: i64 = -32603;
/// The first code in the JSON-RPC server-error range.
pub const SERVER_ERROR: i64 = -32000;

/// States whether an application failure may succeed when retried.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Retryability {
    /// Permits the caller to retry the operation.
    Retryable,
    /// Declares the failure final for this operation.
    Final,
}

/// Describes one invalid IPC value or local session operation.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct IpcError {
    /// Contains the JSON-RPC error code.
    pub code: i64,
    /// Contains a human-readable description.
    pub message: String,
}

impl IpcError {
    /// Creates an IPC error with a JSON-RPC code.
    pub fn new(code: i64, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }
}

impl fmt::Display for IpcError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let Self { code, message } = self;
        write!(formatter, "JSON-RPC error {code}: {message}")
    }
}

impl std::error::Error for IpcError {}

/// Contains a JSON-RPC error response body.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ErrorObject {
    /// Contains the numeric JSON-RPC error code.
    pub code: i64,
    /// Contains the short error message.
    pub message: String,
    /// Contains optional structured details.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub data: Option<Value>,
}

impl ErrorObject {
    /// Creates an error response body.
    pub fn new(code: i64, message: impl Into<String>, data: Option<Value>) -> Self {
        Self {
            code,
            message: message.into(),
            data,
        }
    }

    /// Creates an application error with stable retry details.
    pub fn application(
        code: i64,
        stable_code: impl Into<String>,
        message: impl Into<String>,
        retryability: Retryability,
    ) -> Self {
        let message = message.into();
        let mut data = Map::new();
        data.insert("code".to_string(), Value::String(stable_code.into()));
        let retryable = match retryability {
            Retryability::Retryable => true,
            Retryability::Final => false,
        };
        data.insert("retryable".to_string(), Value::Bool(retryable));
        Self::new(code, message, Some(Value::Object(data)))
    }

    /// Creates a protocol error with a stable detail code.
    pub fn protocol(code: i64, stable_code: impl Into<String>, message: impl Into<String>) -> Self {
        let mut data = Map::new();
        data.insert("code".to_string(), Value::String(stable_code.into()));
        Self::new(code, message, Some(Value::Object(data)))
    }
}

impl From<IpcError> for ErrorObject {
    fn from(error: IpcError) -> Self {
        let IpcError { code, message } = error;
        Self::new(code, message, None)
    }
}
