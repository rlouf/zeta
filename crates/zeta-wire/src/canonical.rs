//! Canonical JSON serialization (spec §2.1).
//!
//! Sorted keys, compact separators, literal UTF-8. serde_json's
//! default object map is a BTreeMap, so serializing a [`Value`]
//! already emits sorted keys; this module exists to give that fact a
//! name and one owner, because canonical bytes are what event ids
//! and the golden vectors are minted from.
//!
//! [`Value`]: serde_json::Value

use serde_json::Value;

/// Serializes a JSON value in the canonical form of spec §2.1.
///
/// # Examples
///
/// ```
/// let value = serde_json::json!({"b": 1, "a": {"z": true, "y": "é"}});
/// assert_eq!(
///     zeta_wire::canonical_json(&value),
///     r#"{"a":{"y":"é","z":true},"b":1}"#
/// );
/// ```
///
/// # Panics
///
/// Panics if the value contains a non-finite float, which valid
/// envelopes cannot hold.
pub fn canonical_json(value: &Value) -> String {
    serde_json::to_string(value).expect("JSON values serialize")
}
