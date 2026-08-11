//! Immutable substrate values and their canonical JSON identity.
//!
//! Objects are Merkle-DAG values, derivations are provenance edges, and refs
//! are named pointers whose conditional updates leave immutable values alone.
//! Identity-bearing JSON uses the shared compact, sorted-key encoding. The same
//! value therefore produces the same address in every implementation.

use std::fmt;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Number, Value};

use crate::domain::{derive, Domain};
use crate::hash::Hash;

/// Identifies one immutable substrate object.
pub type ObjectId = String;

/// Names one mutable substrate pointer.
pub type RefName = String;

/// Reports why a JSON value has no canonical substrate encoding.
///
/// # Examples
///
/// ```
/// let value = serde_json::from_str("18446744073709551616").unwrap();
/// let error = zeta_substrate::canonical_json(&value).unwrap_err();
/// assert!(error.to_string().contains("i64 or u64"));
/// ```
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CanonicalJsonError {
    /// Carries an integer outside the signed-or-unsigned 64-bit range.
    IntegerOutOfRange(String),
    /// Carries a float that has no finite binary64 representation.
    FloatOutOfRange(String),
}

impl fmt::Display for CanonicalJsonError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CanonicalJsonError::IntegerOutOfRange(number) => {
                write!(
                    formatter,
                    "identity-bearing integer {number} must fit i64 or u64"
                )
            }
            CanonicalJsonError::FloatOutOfRange(number) => write!(
                formatter,
                "identity-bearing float {number} must be a finite binary64 value"
            ),
        }
    }
}

impl std::error::Error for CanonicalJsonError {}

/// Stores one immutable value in the substrate Merkle DAG.
///
/// # Examples
///
/// ```
/// use serde_json::json;
/// use zeta_substrate::Object;
///
/// let object = Object {
///     kind: "example.message".to_owned(),
///     schema: "zeta.example.v1".to_owned(),
///     data: serde_json::from_value(json!({"text": "hello"})).unwrap(),
///     links: Vec::new(),
/// };
/// assert!(object.content_address().unwrap().to_string().starts_with("b3:"));
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct Object {
    /// Describes the value's domain role.
    pub kind: String,
    /// Identifies the data schema.
    pub schema: String,
    /// Carries the value's schema-defined payload.
    pub data: Map<String, Value>,
    /// Lists structural edges to other objects.
    pub links: Vec<ObjectId>,
}

impl Object {
    /// Returns the canonical bytes of all identity-bearing fields.
    ///
    /// # Examples
    ///
    /// ```
    /// use serde_json::json;
    /// use zeta_substrate::Object;
    ///
    /// let object = Object {
    ///     kind: "example".to_owned(),
    ///     schema: "v1".to_owned(),
    ///     data: serde_json::from_value(json!({"z": 2, "a": 1})).unwrap(),
    ///     links: Vec::new(),
    /// };
    /// assert_eq!(
    ///     object.canonical_bytes().unwrap(),
    ///     br#"{"data":{"a":1,"z":2},"kind":"example","links":[],"schema":"v1"}"#,
    /// );
    /// ```
    ///
    /// # Errors
    ///
    /// Returns [`CanonicalJsonError`] when an identity-bearing number falls
    /// outside the canonical integer or finite binary64 range.
    pub fn canonical_bytes(&self) -> Result<Vec<u8>, CanonicalJsonError> {
        let value = serde_json::to_value(self)
            .expect("serializing an Object into a JSON value cannot fail");
        canonical_json(&value)
    }

    /// Returns the object's current content address.
    ///
    /// # Examples
    ///
    /// ```
    /// use serde_json::json;
    /// use zeta_substrate::{Domain, Object};
    ///
    /// let object = Object {
    ///     kind: "example".to_owned(),
    ///     schema: "v1".to_owned(),
    ///     data: serde_json::from_value(json!({})).unwrap(),
    ///     links: Vec::new(),
    /// };
    /// assert_eq!(
    ///     object.content_address().unwrap(),
    ///     zeta_substrate::derive(Domain::Object, &object.canonical_bytes().unwrap()),
    /// );
    /// ```
    ///
    /// # Errors
    ///
    /// Returns [`CanonicalJsonError`] when an identity-bearing number falls
    /// outside the canonical integer or finite binary64 range.
    pub fn content_address(&self) -> Result<Hash, CanonicalJsonError> {
        let bytes = self.canonical_bytes()?;
        Ok(derive(Domain::Object, &bytes))
    }
}

/// Records one provenance edge between substrate objects.
///
/// # Examples
///
/// ```
/// use serde_json::json;
/// use zeta_substrate::Derivation;
///
/// let derivation = Derivation {
///     producer: "example:copy@1".to_owned(),
///     output_id: zeta_substrate::hash_bytes(b"output").to_string(),
///     input_ids: vec![zeta_substrate::hash_bytes(b"input").to_string()],
///     params: serde_json::from_value(json!({})).unwrap(),
/// };
/// assert!(derivation
///     .content_address()
///     .unwrap()
///     .to_string()
///     .starts_with("b3:"));
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct Derivation {
    /// Names the operation that produced the output.
    pub producer: String,
    /// Identifies the produced object.
    pub output_id: ObjectId,
    /// Identifies the objects consumed by the operation.
    pub input_ids: Vec<ObjectId>,
    /// Carries stable producer parameters.
    pub params: Map<String, Value>,
}

impl Derivation {
    /// Returns the canonical bytes of all identity-bearing fields.
    ///
    /// # Examples
    ///
    /// ```
    /// use serde_json::json;
    /// use zeta_substrate::Derivation;
    ///
    /// let derivation = Derivation {
    ///     producer: "example:copy@1".to_owned(),
    ///     output_id: zeta_substrate::hash_bytes(b"output").to_string(),
    ///     input_ids: Vec::new(),
    ///     params: serde_json::from_value(json!({})).unwrap(),
    /// };
    /// assert!(derivation.canonical_bytes().unwrap().starts_with(b"{"));
    /// ```
    ///
    /// # Errors
    ///
    /// Returns [`CanonicalJsonError`] when an identity-bearing number falls
    /// outside the canonical integer or finite binary64 range.
    pub fn canonical_bytes(&self) -> Result<Vec<u8>, CanonicalJsonError> {
        let value = serde_json::to_value(self)
            .expect("serializing a Derivation into a JSON value cannot fail");
        canonical_json(&value)
    }

    /// Returns the derivation's current content address.
    ///
    /// # Examples
    ///
    /// ```
    /// use serde_json::json;
    /// use zeta_substrate::{Derivation, Domain};
    ///
    /// let derivation = Derivation {
    ///     producer: "example:copy@1".to_owned(),
    ///     output_id: zeta_substrate::hash_bytes(b"output").to_string(),
    ///     input_ids: Vec::new(),
    ///     params: serde_json::from_value(json!({})).unwrap(),
    /// };
    /// assert_eq!(
    ///     derivation.content_address().unwrap(),
    ///     zeta_substrate::derive(Domain::Derivation, &derivation.canonical_bytes().unwrap()),
    /// );
    /// ```
    ///
    /// # Errors
    ///
    /// Returns [`CanonicalJsonError`] when an identity-bearing number falls
    /// outside the canonical integer or finite binary64 range.
    pub fn content_address(&self) -> Result<Hash, CanonicalJsonError> {
        let bytes = self.canonical_bytes()?;
        Ok(derive(Domain::Derivation, &bytes))
    }
}

/// Points one stable name at an immutable object.
///
/// # Examples
///
/// ```
/// let reference = zeta_substrate::Ref {
///     name: "session/head".to_owned(),
///     object_id: zeta_substrate::hash_bytes(b"head").to_string(),
/// };
/// assert_eq!(reference.name, "session/head");
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
pub struct Ref {
    /// Carries the stable pointer name.
    pub name: RefName,
    /// Identifies the current target.
    pub object_id: ObjectId,
}

/// Reports the result of one conditional ref move.
///
/// # Examples
///
/// ```
/// let update = zeta_substrate::RefUpdate {
///     name: "session/head".to_owned(),
///     old_object_id: None,
///     new_object_id: zeta_substrate::hash_bytes(b"head").to_string(),
///     updated: true,
/// };
/// assert!(update.updated);
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
pub struct RefUpdate {
    /// Carries the stable pointer name.
    pub name: RefName,
    /// Reports the target observed before the move.
    pub old_object_id: Option<ObjectId>,
    /// Identifies the requested target.
    pub new_object_id: ObjectId,
    /// Reports whether the conditional move succeeded.
    pub updated: bool,
}

/// Encodes a JSON value with the substrate's canonical byte representation.
///
/// Object keys use Unicode code-point order, separators carry no whitespace,
/// non-ASCII text stays literal, and the output has no trailing newline.
/// Identity-bearing integers must fit the union of i64 and u64, and floats
/// must have a finite binary64 representation.
///
/// # Examples
///
/// ```
/// use serde_json::json;
///
/// let value = json!({"z": 2, "é": "café", "a": 1});
/// assert_eq!(
///     zeta_substrate::canonical_json(&value).unwrap(),
///     "{\"a\":1,\"z\":2,\"é\":\"café\"}".as_bytes(),
/// );
/// ```
///
/// # Errors
///
/// Returns [`CanonicalJsonError`] when an identity-bearing number falls
/// outside the canonical integer or finite binary64 range.
pub fn canonical_json(value: &Value) -> Result<Vec<u8>, CanonicalJsonError> {
    let mut output = Vec::new();
    write_value(value, &mut output)?;
    Ok(output)
}

fn write_value(value: &Value, output: &mut Vec<u8>) -> Result<(), CanonicalJsonError> {
    match value {
        Value::Null => output.extend_from_slice(b"null"),
        Value::Bool(true) => output.extend_from_slice(b"true"),
        Value::Bool(false) => output.extend_from_slice(b"false"),
        Value::Number(number) => {
            let number = python_number(number)?;
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
                write_value(value, output)?;
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
                let value = &values[key];
                write_value(value, output)?;
            }
            output.push(b'}');
        }
    }
    Ok(())
}

fn write_string(text: &str, output: &mut Vec<u8>) {
    let text = serde_json::to_string(text).expect("serializing a JSON string cannot fail");
    output.extend_from_slice(text.as_bytes());
}

fn python_number(number: &Number) -> Result<String, CanonicalJsonError> {
    if number.is_i64() || number.is_u64() {
        return Ok(number.to_string());
    }
    if !number.is_f64() {
        let number = number.to_string();
        for character in number.chars() {
            if character == '.' || character == 'e' || character == 'E' {
                return Err(CanonicalJsonError::FloatOutOfRange(number));
            }
        }
        return Err(CanonicalJsonError::IntegerOutOfRange(number));
    }
    let number = number
        .as_f64()
        .expect("is_f64 guarantees a finite binary64 value");
    let text = Number::from_f64(number)
        .expect("is_f64 guarantees a finite binary64 value")
        .to_string();
    let Some((mantissa, exponent)) = text.split_once('e') else {
        return Ok(fixed_float(text));
    };
    let exponent = exponent
        .parse::<i32>()
        .expect("serde_json renders a valid decimal exponent");
    let sign = if exponent < 0 { '-' } else { '+' };
    Ok(format!("{mantissa}e{sign}{:02}", exponent.unsigned_abs()))
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
