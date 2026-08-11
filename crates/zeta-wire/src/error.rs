//! Protocol errors carrying the conformance rule token.
//!
//! Every rejection names the rule it enforces. The tokens are the
//! same strings the golden vectors document in their `.reason.txt`
//! files, so a conformance test compares tokens instead of guessing
//! at prose. Tokens are data shared across implementations, which is
//! why they are strings and not an enum: the vector files are the
//! authority, and both languages must agree with them, not with a
//! private type.

use std::fmt;

/// One wire-v0 rule violation: a stable token plus a human message.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WireError {
    /// The stable rule token, e.g. `payload_choice`.
    pub rule: String,
    /// A human-readable description of the violation.
    pub message: String,
}

impl WireError {
    /// Creates an error with an explicit rule token.
    pub fn new(rule: &str, message: impl Into<String>) -> Self {
        WireError {
            rule: rule.to_string(),
            message: message.into(),
        }
    }

    /// Creates a `missing_field:<name>` error.
    pub fn missing(field: &str, envelope_kind: &str) -> Self {
        WireError {
            rule: format!("missing_field:{field}"),
            message: format!("a {envelope_kind} must carry `{field}`"),
        }
    }

    /// Creates a `bad_<name>` error.
    pub fn bad(field: &str, message: impl Into<String>) -> Self {
        WireError {
            rule: format!("bad_{field}"),
            message: message.into(),
        }
    }
}

impl fmt::Display for WireError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let WireError { rule, message } = self;
        write!(formatter, "{rule}: {message}")
    }
}

impl std::error::Error for WireError {}
