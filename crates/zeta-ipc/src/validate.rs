//! Validation for initialization and fixed IPC method parameters.

use std::collections::BTreeSet;

use serde_json::{Map, Value};

use crate::error::{IpcError, INVALID_PARAMS, INVALID_REQUEST};
use crate::message::{
    InitializeParams, InitializeResult, Message, Role, MAX_INLINE_PAYLOAD_BYTES, PROTOCOL_VERSION,
};

/// Validates the parameters of a parsed protocol message.
///
/// Direct method requests use the provider parameter shape. Role and direction
/// gates require an initialized [`crate::Session`] and are not checked here.
///
/// # Errors
///
/// Returns [`IpcError`] when a fixed method, initialization request, event
/// notification, or direct provider request has invalid parameters.
pub fn validate_message(message: &Message) -> Result<(), IpcError> {
    match message {
        Message::Request(request) => {
            if request.method == "initialize" {
                parse_initialize_params(&request.params)?;
                return Ok(());
            }
            if fixed_request(&request.method) {
                return validate_fixed_request(&request.method, &request.params);
            }
            validate_direct_request(&request.params)
        }
        Message::Notification(notification) => {
            if notification.method == "event" {
                return validate_event_notification(&notification.params);
            }
            Ok(())
        }
        Message::Success(_) | Message::Error(_) => Ok(()),
    }
}

pub(crate) fn parse_initialize_params(
    params: &Map<String, Value>,
) -> Result<InitializeParams, IpcError> {
    let params = InitializeParams::from_map(params)?;
    validate_initialize_params(&params)?;
    Ok(params)
}

pub(crate) fn validate_initialize_params(params: &InitializeParams) -> Result<(), IpcError> {
    if params.protocol_versions.is_empty() {
        return invalid_params("`protocol_versions` must be non-empty");
    }
    validate_unique_versions(&params.protocol_versions)?;
    validate_non_empty("peer.name", &params.peer.name)?;
    validate_non_empty("peer.version", &params.peer.version)?;
    if params.roles.is_empty() {
        return invalid_params("`roles` must be non-empty");
    }
    validate_unique_roles(&params.roles)?;
    let source = contains_role(&params.roles, Role::Source);
    let provider = contains_role(&params.roles, Role::Provider);
    match (&params.event_types, source) {
        (Some(event_types), true) => validate_event_types(event_types)?,
        (None, true) => return invalid_params("the source role requires `event_types`"),
        (Some(_), false) => return invalid_params("`event_types` requires the source role"),
        (None, false) => {}
    }
    match (&params.methods, provider) {
        (Some(methods), true) => validate_methods(methods)?,
        (None, true) => return invalid_params("the provider role requires `methods`"),
        (Some(_), false) => return invalid_params("`methods` requires the provider role"),
        (None, false) => {}
    }
    if let Some(seconds) = params.heartbeat_seconds {
        if !seconds.is_finite() || !(1.0..=300.0).contains(&seconds) {
            return invalid_params("`heartbeat_seconds` must be a number in [1, 300]");
        }
    }
    if let Some(limit) = params.max_in_flight {
        if !(1..=1024).contains(&limit) {
            return invalid_params("`max_in_flight` must be an integer in [1, 1024]");
        }
    }
    Ok(())
}

pub(crate) fn validate_initialize_result(
    result: &InitializeResult,
    expected_roles: &[Role],
) -> Result<(), IpcError> {
    if result.protocol_version != PROTOCOL_VERSION {
        return Err(IpcError::new(
            INVALID_REQUEST,
            format!(
                "the runtime selected unsupported protocol version {}",
                result.protocol_version
            ),
        ));
    }
    validate_non_empty_request("runtime.name", &result.runtime.name)?;
    validate_non_empty_request("runtime.version", &result.runtime.version)?;
    validate_unique_roles_for_result(&result.roles)?;
    if result.roles.len() != expected_roles.len() {
        return Err(IpcError::new(
            INVALID_REQUEST,
            "the runtime did not accept the requested roles exactly",
        ));
    }
    for expected in expected_roles {
        if !contains_role(&result.roles, *expected) {
            return Err(IpcError::new(
                INVALID_REQUEST,
                "the runtime did not accept the requested roles exactly",
            ));
        }
    }
    if !result.heartbeat_seconds.is_finite() || !(1.0..=300.0).contains(&result.heartbeat_seconds) {
        return Err(IpcError::new(
            INVALID_REQUEST,
            "the runtime returned an invalid heartbeat interval",
        ));
    }
    if !(1..=1024).contains(&result.max_in_flight) {
        return Err(IpcError::new(
            INVALID_REQUEST,
            "the runtime returned an invalid in-flight limit",
        ));
    }
    Ok(())
}

fn validate_unique_versions(versions: &[u64]) -> Result<(), IpcError> {
    let mut seen = BTreeSet::new();
    for version in versions {
        if !seen.insert(*version) {
            return invalid_params("`protocol_versions` must not contain duplicates");
        }
    }
    Ok(())
}

fn validate_unique_roles(roles: &[Role]) -> Result<(), IpcError> {
    let mut seen = BTreeSet::new();
    for role in roles {
        if !seen.insert(*role) {
            return invalid_params("`roles` must not contain duplicates");
        }
    }
    Ok(())
}

fn validate_unique_roles_for_result(roles: &[Role]) -> Result<(), IpcError> {
    let mut seen = BTreeSet::new();
    for role in roles {
        if !seen.insert(*role) {
            return Err(IpcError::new(
                INVALID_REQUEST,
                "the runtime returned duplicate roles",
            ));
        }
    }
    Ok(())
}

fn validate_event_types(event_types: &[crate::message::EventTypeDecl]) -> Result<(), IpcError> {
    let mut seen = BTreeSet::new();
    for event_type in event_types {
        validate_non_empty("event type", &event_type.event_type)?;
        validate_non_empty("event schema", &event_type.schema)?;
        if !seen.insert(event_type.event_type.as_str()) {
            return invalid_params("`event_types` must not contain duplicate types");
        }
    }
    Ok(())
}

fn validate_methods(methods: &[crate::message::MethodDecl]) -> Result<(), IpcError> {
    let mut seen = BTreeSet::new();
    for method in methods {
        validate_non_empty("method name", &method.name)?;
        if method_is_reserved(&method.name) {
            return invalid_params(format!("method {:?} uses a reserved name", method.name));
        }
        if !seen.insert(method.name.as_str()) {
            return invalid_params("`methods` must not contain duplicate names");
        }
    }
    Ok(())
}

pub(crate) fn method_is_reserved(method: &str) -> bool {
    method == "initialize"
        || method == "event"
        || method == "ping"
        || method == "shutdown"
        || method == "agents.list"
        || method == "project.reload"
        || method == "runtime.status"
        || method.starts_with("events.")
        || method.starts_with("session.")
        || method.starts_with("rpc.")
}

pub(crate) fn validate_fixed_request(
    method: &str,
    params: &Map<String, Value>,
) -> Result<(), IpcError> {
    match method {
        "events.publish" => validate_publish(params),
        "events.list" => validate_events_list(params),
        "agents.list" | "project.reload" | "runtime.status" => validate_empty(params),
        "session.start" => validate_session_start(params),
        "session.send" => validate_session_send(params),
        "session.status" => validate_session_status(params),
        "session.list" => validate_empty(params),
        "session.cancel" => validate_session_cancel(params),
        "ping" => validate_empty(params),
        "shutdown" => validate_shutdown(params),
        "initialize" | "event" => invalid_params(format!(
            "{method:?} is not valid through the ordinary request path"
        )),
        _ => Ok(()),
    }
}

pub(crate) fn validate_direct_request(params: &Map<String, Value>) -> Result<(), IpcError> {
    validate_keys(params, &["input", "base_dir", "effect_key"], &["input"])?;
    let Some(input) = params.get("input") else {
        unreachable!("validate_keys checked input");
    };
    if !input.is_object() {
        return invalid_params("`input` must be an object");
    }
    optional_string(params, "base_dir")?;
    optional_string(params, "effect_key")?;
    Ok(())
}

pub(crate) fn validate_event_notification(params: &Map<String, Value>) -> Result<(), IpcError> {
    validate_keys(params, &["event"], &["event"])?;
    let Some(event) = params.get("event") else {
        unreachable!("validate_keys checked event");
    };
    validate_durable_event(event)
}

pub(crate) fn validate_result(method: &str, result: &Value) -> Result<(), IpcError> {
    if !result.is_object() {
        return Err(IpcError::new(
            INVALID_REQUEST,
            format!("the result of {method:?} must be an object"),
        ));
    }
    Ok(())
}

fn validate_publish(params: &Map<String, Value>) -> Result<(), IpcError> {
    validate_keys(
        params,
        &[
            "type",
            "payload",
            "idempotency_key",
            "caused_by",
            "session_id",
            "run_id",
            "turn_id",
        ],
        &["type", "payload"],
    )?;
    required_string(params, "type")?;
    let Some(payload) = params.get("payload") else {
        unreachable!("validate_keys checked payload");
    };
    if !payload.is_object() {
        return invalid_params("`payload` must be an object");
    }
    let payload = serde_json::to_vec(payload).expect("JSON values serialize");
    if payload.len() > MAX_INLINE_PAYLOAD_BYTES {
        return invalid_params("`payload` exceeds the 64 KiB inline limit");
    }
    for field in [
        "idempotency_key",
        "caused_by",
        "session_id",
        "run_id",
        "turn_id",
    ] {
        optional_string(params, field)?;
    }
    Ok(())
}

fn validate_events_list(params: &Map<String, Value>) -> Result<(), IpcError> {
    validate_keys(
        params,
        &[
            "event_type",
            "event_type_prefix",
            "session_id",
            "run_id",
            "turn_id",
            "caused_by",
            "after_cursor",
            "limit",
            "newest_first",
        ],
        &[],
    )?;
    for field in [
        "event_type",
        "event_type_prefix",
        "session_id",
        "run_id",
        "turn_id",
        "caused_by",
    ] {
        optional_string(params, field)?;
    }
    for field in ["after_cursor", "limit"] {
        if let Some(value) = params.get(field) {
            if value.as_u64().is_none() {
                return invalid_params(format!("`{field}` must be a non-negative integer"));
            }
        }
    }
    if let Some(value) = params.get("newest_first") {
        if !value.is_boolean() {
            return invalid_params("`newest_first` must be a boolean");
        }
    }
    Ok(())
}

fn validate_session_start(params: &Map<String, Value>) -> Result<(), IpcError> {
    validate_keys(params, &["message", "idempotency_key"], &["message"])?;
    required_string(params, "message")?;
    optional_string(params, "idempotency_key")?;
    Ok(())
}

fn validate_session_send(params: &Map<String, Value>) -> Result<(), IpcError> {
    validate_keys(
        params,
        &["session_id", "message", "idempotency_key"],
        &["session_id", "message"],
    )?;
    required_string(params, "session_id")?;
    required_string(params, "message")?;
    optional_string(params, "idempotency_key")?;
    Ok(())
}

fn validate_session_status(params: &Map<String, Value>) -> Result<(), IpcError> {
    validate_keys(params, &["session_id"], &["session_id"])?;
    required_string(params, "session_id")?;
    Ok(())
}

fn validate_session_cancel(params: &Map<String, Value>) -> Result<(), IpcError> {
    validate_keys(params, &["run_id", "session_id", "reason"], &["run_id"])?;
    required_string(params, "run_id")?;
    optional_string(params, "session_id")?;
    optional_string(params, "reason")?;
    Ok(())
}

fn validate_shutdown(params: &Map<String, Value>) -> Result<(), IpcError> {
    validate_keys(params, &["reason"], &[])?;
    optional_string(params, "reason")?;
    Ok(())
}

fn validate_empty(params: &Map<String, Value>) -> Result<(), IpcError> {
    validate_keys(params, &[], &[])
}

fn validate_durable_event(value: &Value) -> Result<(), IpcError> {
    let Value::Object(event) = value else {
        return invalid_params("`event` must be an object");
    };
    for field in [
        "id",
        "type",
        "source",
        "payload",
        "idempotency_key",
        "caused_by",
        "session_id",
        "run_id",
        "turn_id",
        "timestamp_ms",
        "cursor",
    ] {
        if !event.contains_key(field) {
            return invalid_params(format!("missing event field {field:?}"));
        }
    }
    for field in ["id", "type", "source"] {
        required_string(event, field)?;
    }
    let Some(payload) = event.get("payload") else {
        unreachable!("validate_keys checked payload");
    };
    if !payload.is_object() {
        return invalid_params("`event.payload` must be an object");
    }
    for field in [
        "idempotency_key",
        "caused_by",
        "session_id",
        "run_id",
        "turn_id",
    ] {
        nullable_string(event, field)?;
    }
    for field in ["timestamp_ms", "cursor"] {
        let Some(value) = event.get(field) else {
            unreachable!("validate_keys checked integer field");
        };
        if value.as_u64().is_none() {
            return invalid_params(format!("`event.{field}` must be a non-negative integer"));
        }
    }
    Ok(())
}

fn validate_keys(
    params: &Map<String, Value>,
    allowed: &[&str],
    required: &[&str],
) -> Result<(), IpcError> {
    for key in params.keys() {
        if !allowed.contains(&key.as_str()) {
            return invalid_params(format!("unsupported parameter {key:?}"));
        }
    }
    for key in required {
        if !params.contains_key(*key) {
            return invalid_params(format!("missing parameter {key:?}"));
        }
    }
    Ok(())
}

fn required_string(params: &Map<String, Value>, field: &str) -> Result<(), IpcError> {
    let Some(value) = params.get(field).and_then(Value::as_str) else {
        return invalid_params(format!("`{field}` must be a non-empty string"));
    };
    validate_non_empty(field, value)
}

fn optional_string(params: &Map<String, Value>, field: &str) -> Result<(), IpcError> {
    let Some(value) = params.get(field) else {
        return Ok(());
    };
    if value.is_null() {
        return Ok(());
    }
    let Some(value) = value.as_str() else {
        return invalid_params(format!("`{field}` must be null or a non-empty string"));
    };
    validate_non_empty(field, value)
}

fn nullable_string(params: &Map<String, Value>, field: &str) -> Result<(), IpcError> {
    let Some(value) = params.get(field) else {
        return invalid_params(format!("missing parameter {field:?}"));
    };
    if value.is_null() {
        return Ok(());
    }
    let Some(value) = value.as_str() else {
        return invalid_params(format!("`{field}` must be null or a non-empty string"));
    };
    validate_non_empty(field, value)
}

fn validate_non_empty(field: &str, value: &str) -> Result<(), IpcError> {
    if value.is_empty() {
        return invalid_params(format!("`{field}` must be non-empty"));
    }
    Ok(())
}

fn validate_non_empty_request(field: &str, value: &str) -> Result<(), IpcError> {
    if value.is_empty() {
        return Err(IpcError::new(
            INVALID_REQUEST,
            format!("`{field}` must be non-empty"),
        ));
    }
    Ok(())
}

fn contains_role(roles: &[Role], expected: Role) -> bool {
    for role in roles {
        if *role == expected {
            return true;
        }
    }
    false
}

fn invalid_params<T>(message: impl Into<String>) -> Result<T, IpcError> {
    Err(IpcError::new(INVALID_PARAMS, message))
}

fn fixed_request(method: &str) -> bool {
    method == "events.publish"
        || method == "events.list"
        || method == "runtime.status"
        || method == "session.start"
        || method == "session.send"
        || method == "session.status"
        || method == "session.list"
        || method == "session.cancel"
        || method == "ping"
        || method == "shutdown"
}
