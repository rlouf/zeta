//! Typed JSON-RPC 2.0 messages and IPC initialization values.

use std::fmt;

use serde::de::Error as _;
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::{Map, Value};

use crate::error::{ErrorObject, IpcError, INVALID_PARAMS, INVALID_REQUEST, PARSE_ERROR};

/// Contains the JSON-RPC version used by every message.
pub const JSONRPC_VERSION: &str = "2.0";
/// Contains the IPC protocol version implemented by this crate.
pub const PROTOCOL_VERSION: u64 = 0;
/// Contains the maximum compact size of an inline event payload.
pub const MAX_INLINE_PAYLOAD_BYTES: usize = 64 * 1024;

/// Identifies one JSON-RPC request in its sender's namespace.
#[derive(Clone, Debug, Hash, PartialEq, Eq, PartialOrd, Ord)]
pub enum RequestId {
    /// Contains a string request id.
    String(String),
    /// Contains a negative integral request id.
    Signed(i64),
    /// Contains a non-negative integral request id.
    Unsigned(u64),
}

impl RequestId {
    /// Parses a non-null string or integral JSON number.
    ///
    /// # Errors
    ///
    /// Returns [`IpcError`] when the value is null, fractional, or another
    /// JSON type.
    pub fn parse(value: &Value) -> Result<Self, IpcError> {
        match value {
            Value::String(text) => Ok(Self::String(text.clone())),
            Value::Number(number) => {
                if let Some(number) = number.as_u64() {
                    return Ok(Self::Unsigned(number));
                }
                let Some(number) = number.as_i64() else {
                    return Err(IpcError::new(
                        INVALID_REQUEST,
                        "a request id must be a string or integral number",
                    ));
                };
                Ok(Self::Signed(number))
            }
            Value::Null | Value::Bool(_) | Value::Array(_) | Value::Object(_) => {
                Err(IpcError::new(
                    INVALID_REQUEST,
                    "a request id must be a non-null string or integral number",
                ))
            }
        }
    }

    pub(crate) fn to_value(&self) -> Value {
        match self {
            Self::String(text) => Value::String(text.clone()),
            Self::Signed(number) => Value::from(*number),
            Self::Unsigned(number) => Value::from(*number),
        }
    }
}

impl From<&str> for RequestId {
    fn from(value: &str) -> Self {
        Self::String(value.to_string())
    }
}

impl From<String> for RequestId {
    fn from(value: String) -> Self {
        Self::String(value)
    }
}

impl From<i64> for RequestId {
    fn from(value: i64) -> Self {
        if value < 0 {
            Self::Signed(value)
        } else {
            Self::Unsigned(value as u64)
        }
    }
}

impl From<u64> for RequestId {
    fn from(value: u64) -> Self {
        Self::Unsigned(value)
    }
}

impl fmt::Display for RequestId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::String(text) => formatter.write_str(text),
            Self::Signed(number) => write!(formatter, "{number}"),
            Self::Unsigned(number) => write!(formatter, "{number}"),
        }
    }
}

impl Serialize for RequestId {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        match self {
            Self::String(text) => serializer.serialize_str(text),
            Self::Signed(number) => serializer.serialize_i64(*number),
            Self::Unsigned(number) => serializer.serialize_u64(*number),
        }
    }
}

impl<'de> Deserialize<'de> for RequestId {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let value = Value::deserialize(deserializer)?;
        Self::parse(&value).map_err(D::Error::custom)
    }
}

/// Contains one JSON-RPC request.
#[derive(Clone, Debug, PartialEq)]
pub struct Request {
    /// Identifies the request in the sender's namespace.
    pub id: RequestId,
    /// Names the method to invoke.
    pub method: String,
    /// Contains the method parameters.
    pub params: Map<String, Value>,
}

impl Request {
    /// Creates a request with object parameters.
    pub fn new(id: RequestId, method: impl Into<String>, params: Map<String, Value>) -> Self {
        Self {
            id,
            method: method.into(),
            params,
        }
    }
}

/// Contains one JSON-RPC notification.
#[derive(Clone, Debug, PartialEq)]
pub struct Notification {
    /// Names the notification method.
    pub method: String,
    /// Contains the notification parameters.
    pub params: Map<String, Value>,
}

impl Notification {
    /// Creates a notification with object parameters.
    pub fn new(method: impl Into<String>, params: Map<String, Value>) -> Self {
        Self {
            method: method.into(),
            params,
        }
    }
}

/// Contains one successful JSON-RPC response.
#[derive(Clone, Debug, PartialEq)]
pub struct SuccessResponse {
    /// Identifies the request that resolved.
    pub id: RequestId,
    /// Contains the method result.
    pub result: Value,
}

impl SuccessResponse {
    /// Creates a successful response.
    pub fn new(id: RequestId, result: Value) -> Self {
        Self { id, result }
    }
}

/// Contains one failed JSON-RPC response.
#[derive(Clone, Debug, PartialEq)]
pub struct ErrorResponse {
    /// Identifies the request, or is null when no valid id was recoverable.
    pub id: Option<RequestId>,
    /// Describes the failure.
    pub error: ErrorObject,
}

impl ErrorResponse {
    /// Creates a failed response.
    pub fn new(id: Option<RequestId>, error: ErrorObject) -> Self {
        Self { id, error }
    }
}

/// Classifies one validated JSON-RPC message.
#[derive(Clone, Debug, PartialEq)]
pub enum Message {
    /// Carries a request that requires one response.
    Request(Request),
    /// Carries a notification that creates no pending request.
    Notification(Notification),
    /// Carries a successful response.
    Success(SuccessResponse),
    /// Carries a failed response.
    Error(ErrorResponse),
}

impl Message {
    /// Parses and classifies one JSON value.
    ///
    /// Classification uses the `method`, `result`, and `error` members before
    /// constructing a typed message. Unknown members are ignored.
    ///
    /// # Errors
    ///
    /// Returns [`IpcError`] when the value is not a supported JSON-RPC 2.0
    /// message.
    pub fn parse_value(value: &Value) -> Result<Self, IpcError> {
        let Value::Object(fields) = value else {
            return Err(IpcError::new(
                INVALID_REQUEST,
                "a JSON-RPC message must be an object",
            ));
        };
        if fields.get("jsonrpc").and_then(Value::as_str) != Some(JSONRPC_VERSION) {
            return Err(IpcError::new(
                INVALID_REQUEST,
                "a JSON-RPC message must carry `jsonrpc` equal to `2.0`",
            ));
        }
        let has_method = fields.contains_key("method");
        let has_result = fields.contains_key("result");
        let has_error = fields.contains_key("error");
        match (has_method, has_result, has_error) {
            (true, false, false) => parse_call(fields),
            (false, true, false) => parse_success(fields),
            (false, false, true) => parse_error(fields),
            (false, false, false)
            | (false, true, true)
            | (true, false, true)
            | (true, true, false)
            | (true, true, true) => Err(IpcError::new(
                INVALID_REQUEST,
                "a JSON-RPC message must be exactly one request, notification, success, or error",
            )),
        }
    }

    /// Parses and classifies one JSON text.
    ///
    /// # Errors
    ///
    /// Returns the JSON-RPC parse-error code for malformed JSON and an
    /// appropriate shape error for a parsed invalid value.
    pub fn parse_str(text: &str) -> Result<Self, IpcError> {
        let value = serde_json::from_str::<Value>(text);
        let Ok(value) = value else {
            return Err(IpcError::new(PARSE_ERROR, "the line is not valid JSON"));
        };
        Self::parse_value(&value)
    }

    /// Converts the message to a JSON value.
    pub fn to_value(&self) -> Value {
        let mut fields = Map::new();
        fields.insert(
            "jsonrpc".to_string(),
            Value::String(JSONRPC_VERSION.to_string()),
        );
        match self {
            Self::Request(Request { id, method, params }) => {
                fields.insert("id".to_string(), id.to_value());
                fields.insert("method".to_string(), Value::String(method.clone()));
                fields.insert("params".to_string(), Value::Object(params.clone()));
            }
            Self::Notification(Notification { method, params }) => {
                fields.insert("method".to_string(), Value::String(method.clone()));
                fields.insert("params".to_string(), Value::Object(params.clone()));
            }
            Self::Success(SuccessResponse { id, result }) => {
                fields.insert("id".to_string(), id.to_value());
                fields.insert("result".to_string(), result.clone());
            }
            Self::Error(ErrorResponse { id, error }) => {
                let id = match id {
                    Some(id) => id.to_value(),
                    None => Value::Null,
                };
                fields.insert("id".to_string(), id);
                let mut error_fields = Map::new();
                error_fields.insert("code".to_string(), Value::from(error.code));
                error_fields.insert("message".to_string(), Value::String(error.message.clone()));
                if let Some(data) = &error.data {
                    error_fields.insert("data".to_string(), data.clone());
                }
                fields.insert("error".to_string(), Value::Object(error_fields));
            }
        }
        Value::Object(fields)
    }

    /// Serializes the message as compact JSON.
    pub fn to_json(&self) -> String {
        self.to_value().to_string()
    }
}

fn parse_call(fields: &Map<String, Value>) -> Result<Message, IpcError> {
    let Some(method) = fields.get("method").and_then(Value::as_str) else {
        return Err(IpcError::new(INVALID_REQUEST, "`method` must be a string"));
    };
    if method.is_empty() {
        return Err(IpcError::new(INVALID_REQUEST, "`method` must be non-empty"));
    }
    let params = parse_params(fields.get("params"))?;
    let Some(id) = fields.get("id") else {
        return Ok(Message::Notification(Notification::new(method, params)));
    };
    let id = RequestId::parse(id)?;
    Ok(Message::Request(Request::new(id, method, params)))
}

fn parse_params(value: Option<&Value>) -> Result<Map<String, Value>, IpcError> {
    let Some(value) = value else {
        return Ok(Map::new());
    };
    let Value::Object(params) = value else {
        return Err(IpcError::new(INVALID_REQUEST, "`params` must be an object"));
    };
    Ok(params.clone())
}

fn parse_success(fields: &Map<String, Value>) -> Result<Message, IpcError> {
    let Some(id) = fields.get("id") else {
        return Err(IpcError::new(
            INVALID_REQUEST,
            "a successful response must carry `id`",
        ));
    };
    let id = RequestId::parse(id)?;
    let result = fields
        .get("result")
        .expect("the classifier observed result")
        .clone();
    Ok(Message::Success(SuccessResponse::new(id, result)))
}

fn parse_error(fields: &Map<String, Value>) -> Result<Message, IpcError> {
    let Some(id) = fields.get("id") else {
        return Err(IpcError::new(
            INVALID_REQUEST,
            "an error response must carry `id`",
        ));
    };
    let id = match id {
        Value::Null => None,
        Value::String(_)
        | Value::Number(_)
        | Value::Bool(_)
        | Value::Array(_)
        | Value::Object(_) => Some(RequestId::parse(id)?),
    };
    let error = fields
        .get("error")
        .expect("the classifier observed error")
        .clone();
    let error = serde_json::from_value::<ErrorObject>(error).map_err(|error| {
        IpcError::new(
            INVALID_REQUEST,
            format!("`error` must be a JSON-RPC error object: {error}"),
        )
    })?;
    Ok(Message::Error(ErrorResponse::new(id, error)))
}

/// Describes one logical permission on an initialized connection.
#[derive(Clone, Copy, Debug, Hash, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Role {
    /// Permits publishing declared event types.
    Source,
    /// Permits event queries, session methods, and event notifications.
    Client,
    /// Permits calls to methods declared by the peer.
    Provider,
}

/// Identifies one IPC participant.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PeerIdentity {
    /// Contains the participant name.
    pub name: String,
    /// Contains the participant version.
    pub version: String,
}

impl PeerIdentity {
    /// Creates a participant identity.
    pub fn new(name: impl Into<String>, version: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            version: version.into(),
        }
    }
}

/// Declares one event type that a source may publish.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EventTypeDecl {
    /// Contains the durable event type.
    #[serde(rename = "type")]
    pub event_type: String,
    /// Names the event payload schema.
    pub schema: String,
}

impl EventTypeDecl {
    /// Creates an event-type declaration.
    pub fn new(event_type: impl Into<String>, schema: impl Into<String>) -> Self {
        Self {
            event_type: event_type.into(),
            schema: schema.into(),
        }
    }
}

/// Declares one direct JSON-RPC method that a provider serves.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MethodDecl {
    /// Contains the direct method name.
    pub name: String,
}

impl MethodDecl {
    /// Creates a direct-method declaration.
    pub fn new(name: impl Into<String>) -> Self {
        Self { name: name.into() }
    }
}

/// Contains the parameters of the initial `initialize` request.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InitializeParams {
    /// Lists the protocol versions understood by the peer.
    pub protocol_versions: Vec<u64>,
    /// Identifies the peer.
    pub peer: PeerIdentity,
    /// Lists the requested roles.
    pub roles: Vec<Role>,
    /// Lists the event types available to the source role.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub event_types: Option<Vec<EventTypeDecl>>,
    /// Lists the direct methods available through the provider role.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub methods: Option<Vec<MethodDecl>>,
    /// Requests a heartbeat interval in seconds.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub heartbeat_seconds: Option<f64>,
    /// Requests an unanswered-request limit.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_in_flight: Option<u64>,
}

impl InitializeParams {
    pub(crate) fn to_map(&self) -> Map<String, Value> {
        let value = serde_json::to_value(self).expect("initialization parameters serialize");
        let Value::Object(params) = value else {
            unreachable!("initialization parameters serialize as an object");
        };
        params
    }

    pub(crate) fn from_map(params: &Map<String, Value>) -> Result<Self, IpcError> {
        serde_json::from_value(Value::Object(params.clone())).map_err(|error| {
            IpcError::new(
                INVALID_PARAMS,
                format!("invalid initialize parameters: {error}"),
            )
        })
    }
}

/// Contains the result of a successful initialization.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InitializeResult {
    /// Contains the selected IPC protocol version.
    pub protocol_version: u64,
    /// Identifies the runtime.
    pub runtime: PeerIdentity,
    /// Lists the accepted roles.
    pub roles: Vec<Role>,
    /// Contains non-secret settings for the peer.
    pub config: Map<String, Value>,
    /// Contains the effective heartbeat interval in seconds.
    pub heartbeat_seconds: f64,
    /// Contains the effective unanswered-request limit.
    pub max_in_flight: u64,
}

impl InitializeResult {
    pub(crate) fn to_value(&self) -> Value {
        serde_json::to_value(self).expect("initialization results serialize")
    }

    pub(crate) fn from_value(value: &Value) -> Result<Self, IpcError> {
        serde_json::from_value(value.clone()).map_err(|error| {
            IpcError::new(
                INVALID_REQUEST,
                format!("invalid initialize result: {error}"),
            )
        })
    }
}
