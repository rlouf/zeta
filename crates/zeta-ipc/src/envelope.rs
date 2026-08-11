//! Typed wire-v0 envelopes.
//!
//! Parsing validates the raw JSON first (see [`validate_envelope`]),
//! so these types never observe malformed input. Unknown fields are
//! ignored per spec but preserved in `extra`, so an envelope that
//! parses re-serializes to its exact canonical bytes — the vectors
//! test that property directly. Nullable-but-required fields
//! (`caused_by`, `session_id`) are plain [`Option`]s. `None` serializes as
//! `null`, which is the canonical form.
//!
//! [`validate_envelope`]: crate::validate_envelope

use serde::{Deserialize, Serialize};
use serde_json::{Map, Number, Value};

use crate::canonical::canonical_json;
use crate::error::WireError;
use crate::validate::validate_envelope;

/// The wire protocol version this crate speaks.
pub const PROTOCOL_VERSION: u64 = 0;

/// The inline-payload ceiling in canonical bytes (spec §6).
pub const MAX_INLINE_PAYLOAD_BYTES: usize = 64 * 1024;

/// The fields every envelope carries (spec §3).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Common {
    pub v: u64,
    pub id: String,
    pub ts: String,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

impl Common {
    /// Creates the common fields for a freshly built envelope.
    pub fn new(id: impl Into<String>, ts: impl Into<String>) -> Self {
        Common {
            v: PROTOCOL_VERSION,
            id: id.into(),
            ts: ts.into(),
            extra: Map::new(),
        }
    }
}

/// The handshake opener, child to parent (spec §5.1).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Hello {
    #[serde(flatten)]
    pub common: Common,
    pub name: String,
    pub plugin_version: String,
    pub role: String,
    pub protocol_versions: Vec<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub event_types: Option<Vec<EventTypeDecl>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub operations: Option<Vec<OperationDecl>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub capabilities: Option<Map<String, Value>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub heartbeat_secs: Option<Number>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ack_window: Option<u64>,
}

/// One declared event type in a `hello` (spec §5.3).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct EventTypeDecl {
    #[serde(rename = "type")]
    pub event_type: String,
    pub schema: String,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

/// One declared operation in a `hello` (spec §5.4).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct OperationDecl {
    pub name: String,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

/// The handshake closer, parent to child (spec §5.6).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct HelloAck {
    #[serde(flatten)]
    pub common: Common,
    pub protocol_version: u64,
    pub runtime: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub config: Option<Map<String, Value>>,
}

/// One event, child to parent (spec §6).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct EventEnvelope {
    #[serde(flatten)]
    pub common: Common,
    #[serde(rename = "type")]
    pub event_type: String,
    pub schema: String,
    pub caused_by: Option<String>,
    pub session_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub payload: Option<Map<String, Value>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub payload_hash: Option<String>,
}

/// One acknowledgment, parent to child (spec §7).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Ack {
    #[serde(flatten)]
    pub common: Common,
    pub event_id: String,
}

/// One liveness beat, child to parent (spec §8).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Heartbeat {
    #[serde(flatten)]
    pub common: Common,
}

/// One error report, either direction (spec §10).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ErrorEnvelope {
    #[serde(flatten)]
    pub common: Common,
    pub code: String,
    pub message: String,
    pub retryable: bool,
}

/// The orderly-stop request, parent to child (spec §9).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Shutdown {
    #[serde(flatten)]
    pub common: Common,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

/// One operation invocation, parent to child (spec §7.1).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Call {
    #[serde(flatten)]
    pub common: Common,
    pub name: String,
    pub payload: Map<String, Value>,
    pub effect_key: String,
}

/// The answer to one call, child to parent (spec §7.1).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct CallResult {
    #[serde(flatten)]
    pub common: Common,
    pub call_id: String,
    pub ok: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub result: Option<Map<String, Value>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<ErrorInfo>,
}

/// The `{code, message, retryable}` shape inside a failed call.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ErrorInfo {
    pub code: String,
    pub message: String,
    pub retryable: bool,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

/// The outcome a call resolved to, surfaced by the runtime session.
#[derive(Clone, Debug, PartialEq)]
pub struct CallInfo {
    pub call_id: String,
    pub outcome: Result<Map<String, Value>, ErrorInfo>,
}

/// The kind discriminant of an envelope.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Hello,
    HelloAck,
    Event,
    Ack,
    Heartbeat,
    Error,
    Shutdown,
    Call,
    CallResult,
}

/// One validated wire-v0 envelope.
///
/// # Examples
///
/// ```
/// use zeta_ipc::Envelope;
///
/// let line = r#"{"id":"m-1","kind":"heartbeat","ts":"2026-08-10T12:00:00Z","v":0}"#;
/// let envelope = Envelope::parse_str(line).unwrap();
/// assert_eq!(zeta_ipc::canonical_json(&envelope.to_value()), line);
/// ```
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Envelope {
    Hello(Hello),
    HelloAck(HelloAck),
    Event(EventEnvelope),
    Ack(Ack),
    Heartbeat(Heartbeat),
    Error(ErrorEnvelope),
    Shutdown(Shutdown),
    Call(Call),
    CallResult(CallResult),
}

impl Envelope {
    /// Validates and parses one JSON value into a typed envelope.
    ///
    /// # Errors
    ///
    /// Returns [`WireError`] carrying the violated rule token.
    pub fn parse_value(value: &Value) -> Result<Self, WireError> {
        validate_envelope(value)?;
        let envelope = serde_json::from_value(value.clone());
        let Ok(envelope) = envelope else {
            return Err(WireError::new(
                "internal",
                "a validated envelope must convert to its typed form",
            ));
        };
        Ok(envelope)
    }

    /// Validates and parses one JSON text into a typed envelope.
    ///
    /// # Errors
    ///
    /// Returns [`WireError`] with rule `bad_json` for malformed JSON,
    /// or the violated envelope rule token otherwise.
    pub fn parse_str(text: &str) -> Result<Self, WireError> {
        let value = serde_json::from_str::<Value>(text);
        let Ok(value) = value else {
            return Err(WireError::new("bad_json", "the line is not valid JSON"));
        };
        Envelope::parse_value(&value)
    }

    /// Converts the envelope back to its JSON value.
    ///
    /// The value reproduces every field the envelope was parsed
    /// from, including unknown ones, so canonical serialization
    /// round-trips byte-for-byte.
    pub fn to_value(&self) -> Value {
        serde_json::to_value(self).expect("envelopes serialize")
    }

    /// Serializes the envelope to canonical JSON (spec §2.1).
    pub fn to_canonical_json(&self) -> String {
        canonical_json(&self.to_value())
    }

    /// Returns the kind discriminant.
    pub fn kind(&self) -> Kind {
        match self {
            Envelope::Hello(_) => Kind::Hello,
            Envelope::HelloAck(_) => Kind::HelloAck,
            Envelope::Event(_) => Kind::Event,
            Envelope::Ack(_) => Kind::Ack,
            Envelope::Heartbeat(_) => Kind::Heartbeat,
            Envelope::Error(_) => Kind::Error,
            Envelope::Shutdown(_) => Kind::Shutdown,
            Envelope::Call(_) => Kind::Call,
            Envelope::CallResult(_) => Kind::CallResult,
        }
    }

    /// Returns the message id.
    pub fn id(&self) -> &str {
        &self.common().id
    }

    fn common(&self) -> &Common {
        match self {
            Envelope::Hello(Hello { common, .. }) => common,
            Envelope::HelloAck(HelloAck { common, .. }) => common,
            Envelope::Event(EventEnvelope { common, .. }) => common,
            Envelope::Ack(Ack { common, .. }) => common,
            Envelope::Heartbeat(Heartbeat { common }) => common,
            Envelope::Error(ErrorEnvelope { common, .. }) => common,
            Envelope::Shutdown(Shutdown { common, .. }) => common,
            Envelope::Call(Call { common, .. }) => common,
            Envelope::CallResult(CallResult { common, .. }) => common,
        }
    }
}
