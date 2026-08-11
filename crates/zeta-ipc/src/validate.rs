//! Envelope shape validation with stable rule tokens (spec §3–§10).
//!
//! This is a field-for-field port of the Python validator, in the
//! same check order, because the golden vectors pin the rule token
//! each invalid envelope must fail with. Two implementations that
//! disagree on ordering would disagree on tokens; keeping the order
//! identical is what makes the `.reason.txt` files a shared oracle.

use serde_json::{Map, Value};

use crate::error::WireError;
use crate::timestamp::is_valid_utc_timestamp;
use crate::MAX_INLINE_PAYLOAD_BYTES;

fn present<'a>(fields: &'a Map<String, Value>, name: &str) -> Option<&'a Value> {
    let value = fields.get(name)?;
    if value.is_null() {
        return None;
    }
    Some(value)
}

fn is_b3_address(text: &str) -> bool {
    let Some(digest) = text.strip_prefix("b3:") else {
        return false;
    };
    if digest.len() != 64 {
        return false;
    }
    for byte in digest.bytes() {
        let lowercase_hex = byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte);
        if !lowercase_hex {
            return false;
        }
    }
    true
}

const KINDS: [&str; 9] = [
    "hello",
    "hello_ack",
    "event",
    "ack",
    "heartbeat",
    "error",
    "shutdown",
    "call",
    "call_result",
];
const RESERVED_KINDS: [&str; 1] = ["event_batch"];
const ROLES: [&str; 3] = ["source", "tool", "provider"];

/// Validates one parsed envelope against the wire-v0 shape rules.
///
/// # Errors
///
/// Returns [`WireError`] naming the first violated rule, using the
/// tokens the conformance vectors document.
///
/// # Examples
///
/// ```
/// let value = serde_json::json!({"v": 0, "kind": "nope", "id": "m", "ts": "2026-08-10T12:00:00Z"});
/// let error = zeta_ipc::validate_envelope(&value).unwrap_err();
/// assert_eq!(error.rule, "unknown_kind");
/// ```
pub fn validate_envelope(value: &Value) -> Result<(), WireError> {
    let Value::Object(fields) = value else {
        return Err(WireError::new(
            "not_an_object",
            "an envelope must be a JSON object",
        ));
    };
    let Some(version) = present(fields, "v") else {
        return Err(WireError::new(
            "missing_field:v",
            "an envelope must carry `v`",
        ));
    };
    if version.as_u64().is_none() {
        return Err(WireError::new(
            "bad_version",
            "`v` must be a non-negative integer",
        ));
    }
    let Some(kind) = present(fields, "kind") else {
        return Err(WireError::new(
            "missing_field:kind",
            "an envelope must carry `kind`",
        ));
    };
    let Some(kind) = kind.as_str() else {
        return Err(WireError::new("bad_kind", "`kind` must be a string"));
    };
    let Some(id) = present(fields, "id") else {
        return Err(WireError::new(
            "missing_field:id",
            "an envelope must carry `id`",
        ));
    };
    let id_text = id.as_str();
    if id_text.is_none() || id_text == Some("") {
        return Err(WireError::new("bad_id", "`id` must be a non-empty string"));
    }
    let Some(ts) = present(fields, "ts") else {
        return Err(WireError::new(
            "missing_field:ts",
            "an envelope must carry `ts`",
        ));
    };
    let ts_valid = match ts.as_str() {
        Some(text) => is_valid_utc_timestamp(text),
        None => false,
    };
    if !ts_valid {
        return Err(WireError::new(
            "bad_timestamp",
            "`ts` must be RFC 3339 UTC with the Z designator",
        ));
    }
    if RESERVED_KINDS.contains(&kind) {
        return Err(WireError::new(
            "reserved_kind",
            format!("kind {kind:?} is reserved"),
        ));
    }
    if !KINDS.contains(&kind) {
        return Err(WireError::new(
            "unknown_kind",
            format!("unknown kind {kind:?}"),
        ));
    }
    match kind {
        "hello" => validate_hello(fields),
        "hello_ack" => validate_hello_ack(fields),
        "event" => validate_event(fields),
        "ack" => validate_ack(fields),
        "heartbeat" => Ok(()),
        "error" => validate_error(fields),
        "shutdown" => validate_shutdown(fields),
        "call" => validate_call(fields),
        "call_result" => validate_call_result(fields),
        other => Err(WireError::new(
            "unknown_kind",
            format!("unknown kind {other:?}"),
        )),
    }
}

fn required_string(
    fields: &Map<String, Value>,
    field: &str,
    envelope_kind: &str,
) -> Result<String, WireError> {
    let Some(value) = present(fields, field) else {
        return Err(WireError::missing(field, envelope_kind));
    };
    let text = value.as_str();
    let Some(text) = text else {
        return Err(WireError::bad(
            field,
            format!("`{field}` must be a non-empty string"),
        ));
    };
    if text.is_empty() {
        return Err(WireError::bad(
            field,
            format!("`{field}` must be a non-empty string"),
        ));
    }
    Ok(text.to_string())
}

fn validate_hello(fields: &Map<String, Value>) -> Result<(), WireError> {
    required_string(fields, "name", "hello")?;
    required_string(fields, "plugin_version", "hello")?;
    let role = required_string(fields, "role", "hello")?;
    if !ROLES.contains(&role.as_str()) {
        return Err(WireError::new("bad_role", format!("unknown role {role:?}")));
    }
    let Some(versions) = present(fields, "protocol_versions") else {
        return Err(WireError::missing("protocol_versions", "hello"));
    };
    let versions_valid = match versions.as_array() {
        Some(items) => {
            let mut valid = !items.is_empty();
            for item in items {
                if item.as_u64().is_none() {
                    valid = false;
                    break;
                }
            }
            valid
        }
        None => false,
    };
    if !versions_valid {
        return Err(WireError::new(
            "bad_protocol_versions",
            "`protocol_versions` must be a non-empty array of non-negative integers",
        ));
    }
    if role == "source" {
        validate_event_types(fields)?;
    }
    if let Some(operations) = present(fields, "operations") {
        let operations_valid = match operations.as_array() {
            Some(entries) => {
                let mut valid = true;
                for entry in entries {
                    let name = entry.get("name").and_then(Value::as_str);
                    let named = match name {
                        Some(name) => !name.is_empty(),
                        None => false,
                    };
                    if !named {
                        valid = false;
                        break;
                    }
                }
                valid
            }
            None => false,
        };
        if !operations_valid {
            return Err(WireError::new(
                "bad_operations",
                "`operations` must be an array of {name} objects",
            ));
        }
    }
    if let Some(capabilities) = present(fields, "capabilities") {
        if !capabilities.is_object() {
            return Err(WireError::new(
                "bad_capabilities",
                "`capabilities` must be an object",
            ));
        }
    }
    if let Some(heartbeat) = present(fields, "heartbeat_secs") {
        let in_range = match heartbeat.as_f64() {
            Some(seconds) => (1.0..=300.0).contains(&seconds),
            None => false,
        };
        if !in_range {
            return Err(WireError::new(
                "bad_heartbeat_secs",
                "`heartbeat_secs` must be a number in [1, 300]",
            ));
        }
    }
    if let Some(window) = present(fields, "ack_window") {
        let in_range = match window.as_u64() {
            Some(size) => (1..=1024).contains(&size),
            None => false,
        };
        if !in_range {
            return Err(WireError::new(
                "bad_ack_window",
                "`ack_window` must be an integer in [1, 1024]",
            ));
        }
    }
    Ok(())
}

fn validate_event_types(fields: &Map<String, Value>) -> Result<(), WireError> {
    let Some(event_types) = present(fields, "event_types") else {
        return Err(WireError::new(
            "missing_field:event_types",
            "a source hello must carry `event_types`",
        ));
    };
    let valid = match event_types.as_array() {
        Some(entries) => {
            let mut valid = true;
            for entry in entries {
                let type_ok = match entry.get("type").and_then(Value::as_str) {
                    Some(text) => !text.is_empty(),
                    None => false,
                };
                let schema_ok = match entry.get("schema").and_then(Value::as_str) {
                    Some(text) => !text.is_empty(),
                    None => false,
                };
                if !type_ok || !schema_ok {
                    valid = false;
                    break;
                }
            }
            valid
        }
        None => false,
    };
    if !valid {
        return Err(WireError::new(
            "bad_event_types",
            "`event_types` must be an array of {type, schema} string pairs",
        ));
    }
    Ok(())
}

fn validate_hello_ack(fields: &Map<String, Value>) -> Result<(), WireError> {
    let Some(version) = present(fields, "protocol_version") else {
        return Err(WireError::new(
            "missing_field:protocol_version",
            "a hello_ack must carry `protocol_version`",
        ));
    };
    if version.as_u64().is_none() {
        return Err(WireError::new(
            "bad_protocol_version",
            "`protocol_version` must be a non-negative integer",
        ));
    }
    required_string(fields, "runtime", "hello_ack")?;
    if let Some(config) = present(fields, "config") {
        if !config.is_object() {
            return Err(WireError::new("bad_config", "`config` must be an object"));
        }
    }
    Ok(())
}

fn validate_event(fields: &Map<String, Value>) -> Result<(), WireError> {
    required_string(fields, "type", "event")?;
    required_string(fields, "schema", "event")?;
    for field in ["caused_by", "session_id"] {
        let Some(value) = fields.get(field) else {
            return Err(WireError {
                rule: format!("missing_field:{field}"),
                message: format!("an event must carry `{field}` (null is allowed)"),
            });
        };
        let nullable_string = value.is_null() || value.is_string();
        if !nullable_string {
            return Err(WireError::bad(
                field,
                format!("`{field}` must be a string or null"),
            ));
        }
    }
    let payload = present(fields, "payload");
    let payload_hash = present(fields, "payload_hash");
    let has_payload = payload.is_some();
    let has_hash = payload_hash.is_some();
    if has_payload == has_hash {
        return Err(WireError::new(
            "payload_choice",
            "an event must carry exactly one of `payload` and `payload_hash`",
        ));
    }
    if let Some(payload) = payload {
        if !payload.is_object() {
            return Err(WireError::new("bad_payload", "`payload` must be an object"));
        }
        let serialized = crate::canonical::canonical_json(payload);
        if serialized.len() > MAX_INLINE_PAYLOAD_BYTES {
            return Err(WireError::new(
                "payload_too_large",
                "inline payloads are limited to 64 KiB; use `payload_hash`",
            ));
        }
    }
    if let Some(payload_hash) = payload_hash {
        let well_formed = match payload_hash.as_str() {
            Some(text) => is_b3_address(text),
            None => false,
        };
        if !well_formed {
            return Err(WireError::new(
                "bad_payload_hash",
                "`payload_hash` must be `b3:` plus 64 lowercase hex characters",
            ));
        }
    }
    Ok(())
}

fn validate_ack(fields: &Map<String, Value>) -> Result<(), WireError> {
    let Some(event_id) = present(fields, "event_id") else {
        return Err(WireError::new(
            "missing_field:event_id",
            "an ack must carry `event_id`",
        ));
    };
    let non_empty = match event_id.as_str() {
        Some(text) => !text.is_empty(),
        None => false,
    };
    if !non_empty {
        return Err(WireError::new(
            "bad_event_id",
            "`event_id` must be a non-empty string",
        ));
    }
    Ok(())
}

fn validate_error(fields: &Map<String, Value>) -> Result<(), WireError> {
    required_string(fields, "code", "error")?;
    required_string(fields, "message", "error")?;
    let Some(retryable) = present(fields, "retryable") else {
        return Err(WireError::new(
            "missing_field:retryable",
            "an error must carry `retryable`",
        ));
    };
    if !retryable.is_boolean() {
        return Err(WireError::new(
            "bad_retryable",
            "`retryable` must be a boolean",
        ));
    }
    Ok(())
}

fn validate_shutdown(fields: &Map<String, Value>) -> Result<(), WireError> {
    if let Some(reason) = present(fields, "reason") {
        if !reason.is_string() {
            return Err(WireError::new("bad_reason", "`reason` must be a string"));
        }
    }
    Ok(())
}

fn validate_call(fields: &Map<String, Value>) -> Result<(), WireError> {
    required_string(fields, "name", "call")?;
    required_string(fields, "effect_key", "call")?;
    let Some(payload) = present(fields, "payload") else {
        return Err(WireError::new(
            "missing_field:payload",
            "a call must carry `payload`",
        ));
    };
    if !payload.is_object() {
        return Err(WireError::new("bad_payload", "`payload` must be an object"));
    }
    Ok(())
}

fn validate_call_result(fields: &Map<String, Value>) -> Result<(), WireError> {
    required_string(fields, "call_id", "call_result")?;
    let Some(ok) = present(fields, "ok") else {
        return Err(WireError::new(
            "missing_field:ok",
            "a call_result must carry `ok`",
        ));
    };
    let Some(ok) = ok.as_bool() else {
        return Err(WireError::new("bad_ok", "`ok` must be a boolean"));
    };
    let has_result = match present(fields, "result") {
        Some(result) => result.is_object(),
        None => false,
    };
    if ok && !has_result {
        return Err(WireError::new(
            "result_choice",
            "a successful call_result must carry a `result` object",
        ));
    }
    if !ok {
        let Some(error) = present(fields, "error").and_then(Value::as_object) else {
            return Err(WireError::new(
                "result_choice",
                "a failed call_result must carry an `error` object",
            ));
        };
        let code_ok = error.get("code").and_then(Value::as_str).is_some();
        let message_ok = error.get("message").and_then(Value::as_str).is_some();
        let retryable_ok = error.get("retryable").and_then(Value::as_bool).is_some();
        if !code_ok || !message_ok || !retryable_ok {
            return Err(WireError::new(
                "bad_error",
                "`error` must carry {code, message, retryable}",
            ));
        }
    }
    Ok(())
}
