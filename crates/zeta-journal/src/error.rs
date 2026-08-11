//! Structured append and verification failures.

use std::fmt;

use zeta_substrate::{CanonicalJsonError, Hash};

/// Reports why a new event cannot be appended.
///
/// # Examples
///
/// ```
/// let error = zeta_journal::AppendError::EmptyId;
/// assert_eq!(error.reason(), "empty_id");
/// ```
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AppendError {
    /// The opaque event identity is empty.
    EmptyId,
    /// The event type is empty.
    EmptyEventType,
    /// The producer source is empty.
    EmptySource,
    /// The assigned cursor is not positive.
    CursorZero,
    /// The in-memory cursor range has been exhausted.
    CursorExhausted,
    /// The payload falls outside the canonical JSON value domain.
    PayloadEncoding(CanonicalJsonError),
}

impl AppendError {
    /// Returns a stable machine-readable reason.
    ///
    /// # Examples
    ///
    /// ```
    /// assert_eq!(zeta_journal::AppendError::EmptySource.reason(), "empty_source");
    /// ```
    pub fn reason(&self) -> &'static str {
        match self {
            AppendError::EmptyId => "empty_id",
            AppendError::EmptyEventType => "empty_event_type",
            AppendError::EmptySource => "empty_source",
            AppendError::CursorZero => "cursor_zero",
            AppendError::CursorExhausted => "cursor_exhausted",
            AppendError::PayloadEncoding(_) => "payload_encoding",
        }
    }
}

impl fmt::Display for AppendError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            AppendError::EmptyId => write!(formatter, "event id must not be empty"),
            AppendError::EmptyEventType => {
                write!(formatter, "event type must not be empty")
            }
            AppendError::EmptySource => write!(formatter, "event source must not be empty"),
            AppendError::CursorZero => write!(formatter, "event cursor must be positive"),
            AppendError::CursorExhausted => write!(formatter, "event cursor range is exhausted"),
            AppendError::PayloadEncoding(error) => error.fmt(formatter),
        }
    }
}

impl std::error::Error for AppendError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            AppendError::EmptyId => None,
            AppendError::EmptyEventType => None,
            AppendError::EmptySource => None,
            AppendError::CursorZero => None,
            AppendError::CursorExhausted => None,
            AppendError::PayloadEncoding(error) => Some(error),
        }
    }
}

impl From<CanonicalJsonError> for AppendError {
    fn from(error: CanonicalJsonError) -> Self {
        AppendError::PayloadEncoding(error)
    }
}

/// Classifies the first journal-v0 verification divergence.
///
/// # Examples
///
/// ```
/// let kind = zeta_journal::VerificationErrorKind::DuplicateId;
/// assert_eq!(kind.reason(), "duplicate_id");
/// ```
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum VerificationErrorKind {
    /// Cursors are absent, zero, or not strictly increasing.
    CursorOrder {
        /// Carries the previously accepted cursor.
        previous: Option<u64>,
        /// Carries the cursor on the divergent entry.
        actual: Option<u64>,
    },
    /// An event id has already appeared.
    DuplicateId,
    /// A non-null idempotency key has already appeared.
    DuplicateIdempotencyKey {
        /// Carries the repeated global key.
        key: String,
    },
    /// Payload bytes are invalid or differ from canonical encoding.
    PayloadEncoding,
    /// The stored payload address does not identify the canonical bytes.
    PayloadAddressMismatch {
        /// Carries the recomputed content address.
        expected: Hash,
        /// Carries the stored content address.
        actual: Hash,
    },
    /// The stored predecessor does not name the preceding computed entry.
    PreviousAddressMismatch {
        /// Carries the required predecessor.
        expected: Option<Hash>,
        /// Carries the stored predecessor.
        actual: Option<Hash>,
    },
    /// The stored entry address does not identify the canonical chain bytes.
    EntryAddressMismatch {
        /// Carries the recomputed chain address when encoding succeeded.
        expected: Option<Hash>,
        /// Carries the stored chain address.
        actual: Hash,
    },
    /// The retained head does not match an external anchor.
    ExpectedHeadMismatch {
        /// Carries the trusted external head.
        expected: Option<Hash>,
        /// Carries the retained journal head.
        actual: Option<Hash>,
    },
}

impl VerificationErrorKind {
    /// Returns the exact stable journal-v0 reason.
    ///
    /// # Examples
    ///
    /// ```
    /// let kind = zeta_journal::VerificationErrorKind::PayloadEncoding;
    /// assert_eq!(kind.reason(), "payload_encoding");
    /// ```
    pub fn reason(&self) -> &'static str {
        match self {
            VerificationErrorKind::CursorOrder {
                previous: _,
                actual: _,
            } => "cursor_order",
            VerificationErrorKind::DuplicateId => "duplicate_id",
            VerificationErrorKind::DuplicateIdempotencyKey { key: _ } => {
                "duplicate_idempotency_key"
            }
            VerificationErrorKind::PayloadEncoding => "payload_encoding",
            VerificationErrorKind::PayloadAddressMismatch {
                expected: _,
                actual: _,
            } => "payload_address",
            VerificationErrorKind::PreviousAddressMismatch {
                expected: _,
                actual: _,
            } => "previous_address",
            VerificationErrorKind::EntryAddressMismatch {
                expected: _,
                actual: _,
            } => "entry_address",
            VerificationErrorKind::ExpectedHeadMismatch {
                expected: _,
                actual: _,
            } => "expected_head",
        }
    }
}

/// Locates the first verification divergence and its stable class.
///
/// # Examples
///
/// ```
/// let error = zeta_journal::VerificationError {
///     entries_checked: 0,
///     event_id: Some("evt_example".to_owned()),
///     cursor: Some(1),
///     kind: zeta_journal::VerificationErrorKind::DuplicateId,
/// };
/// assert_eq!(error.reason(), "duplicate_id");
/// ```
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VerificationError {
    /// Counts complete entries accepted before the divergence.
    pub entries_checked: usize,
    /// Names the divergent event when one is available.
    pub event_id: Option<String>,
    /// Names the divergent cursor when one is available.
    pub cursor: Option<u64>,
    /// Carries structured details for the divergence class.
    pub kind: VerificationErrorKind,
}

impl VerificationError {
    /// Returns the exact stable journal-v0 reason.
    ///
    /// # Examples
    ///
    /// ```
    /// let error = zeta_journal::VerificationError {
    ///     entries_checked: 0,
    ///     event_id: None,
    ///     cursor: None,
    ///     kind: zeta_journal::VerificationErrorKind::PayloadEncoding,
    /// };
    /// assert_eq!(error.reason(), "payload_encoding");
    /// ```
    pub fn reason(&self) -> &'static str {
        self.kind.reason()
    }
}

impl fmt::Display for VerificationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let location = match &self.event_id {
            Some(event_id) => event_id.as_str(),
            None => "journal head",
        };
        write!(formatter, "{} at {location}", self.reason())
    }
}

impl std::error::Error for VerificationError {}

/// Reports the verified prefix length and retained head.
///
/// # Examples
///
/// ```
/// let report = zeta_journal::VerificationReport {
///     entries_checked: 0,
///     head: None,
/// };
/// assert_eq!(report.entries_checked, 0);
/// ```
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VerificationReport {
    /// Counts verified entries.
    pub entries_checked: usize,
    /// Carries the retained head, or `None` for an empty journal.
    pub head: Option<Hash>,
}
