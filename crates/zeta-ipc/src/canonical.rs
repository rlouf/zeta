//! Canonical JSON serialization (spec §2.1).
//!
//! Sorted keys, compact separators, literal UTF-8, and the specified
//! finite-number spelling make the same value byte-identical. The writer is
//! explicit so serializer-specific exponent notation cannot change the
//! encoded bytes.

use serde_json::{Number, Value};

/// Serializes a JSON value in the canonical form of spec §2.1.
///
/// # Examples
///
/// ```
/// let value = serde_json::json!({"b": 1, "a": {"z": true, "y": "é"}});
/// assert_eq!(
///     zeta_ipc::canonical_json(&value),
///     r#"{"a":{"y":"é","z":true},"b":1}"#
/// );
/// ```
///
/// # Panics
///
/// Panics if an integer falls outside the union of i64 and u64 or a float has
/// no finite binary64 representation.
pub fn canonical_json(value: &Value) -> String {
    assert!(
        identity_numbers_are_valid(value),
        "identity-bearing numbers must fit i64, u64, or finite f64"
    );
    let mut output = Vec::new();
    write_value(value, &mut output);
    String::from_utf8(output).expect("canonical JSON is valid UTF-8")
}

pub(crate) fn identity_numbers_are_valid(value: &Value) -> bool {
    match value {
        Value::Null | Value::Bool(_) | Value::String(_) => true,
        Value::Number(number) => python_number(number).is_some(),
        Value::Array(values) => {
            for value in values {
                if !identity_numbers_are_valid(value) {
                    return false;
                }
            }
            true
        }
        Value::Object(values) => {
            for value in values.values() {
                if !identity_numbers_are_valid(value) {
                    return false;
                }
            }
            true
        }
    }
}

fn write_value(value: &Value, output: &mut Vec<u8>) {
    match value {
        Value::Null => output.extend_from_slice(b"null"),
        Value::Bool(true) => output.extend_from_slice(b"true"),
        Value::Bool(false) => output.extend_from_slice(b"false"),
        Value::Number(number) => {
            let number = python_number(number)
                .expect("identity_numbers_are_valid checks every number before writing");
            output.extend_from_slice(number.as_bytes());
        }
        Value::String(text) => write_string(text, output),
        Value::Array(values) => {
            output.push(b'[');
            let mut first = true;
            for value in values {
                if !first {
                    output.push(b',');
                }
                first = false;
                write_value(value, output);
            }
            output.push(b']');
        }
        Value::Object(values) => {
            output.push(b'{');
            let mut keys = Vec::with_capacity(values.len());
            for key in values.keys() {
                keys.push(key);
            }
            keys.sort();
            let mut first = true;
            for key in keys {
                if !first {
                    output.push(b',');
                }
                first = false;
                write_string(key, output);
                output.push(b':');
                write_value(&values[key], output);
            }
            output.push(b'}');
        }
    }
}

fn write_string(text: &str, output: &mut Vec<u8>) {
    let text = serde_json::to_string(text).expect("serializing a JSON string cannot fail");
    output.extend_from_slice(text.as_bytes());
}

fn python_number(number: &Number) -> Option<String> {
    if number.is_i64() || number.is_u64() {
        return Some(number.to_string());
    }
    if !number.is_f64() {
        return None;
    }
    let number = number
        .as_f64()
        .expect("is_f64 guarantees a finite binary64 value");
    let text = Number::from_f64(number)
        .expect("is_f64 guarantees a finite binary64 value")
        .to_string();
    let Some((mantissa, exponent)) = text.split_once('e') else {
        return Some(fixed_float(text));
    };
    let exponent = exponent
        .parse::<i32>()
        .expect("serde_json renders a valid decimal exponent");
    let sign = if exponent < 0 { '-' } else { '+' };
    Some(format!("{mantissa}e{sign}{:02}", exponent.unsigned_abs()))
}

fn fixed_float(text: String) -> String {
    let (sign, magnitude) = match text.strip_prefix('-') {
        Some(magnitude) => ("-", magnitude),
        None => ("", text.as_str()),
    };
    let Some(digits) = magnitude.strip_prefix("0.0000") else {
        return text;
    };
    let Some(first) = digits.chars().next() else {
        return text;
    };
    let rest = &digits[first.len_utf8()..];
    if rest.is_empty() {
        return format!("{sign}{first}e-05");
    }
    format!("{sign}{first}.{rest}e-05")
}
