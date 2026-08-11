//! Canonical payload identity, journal chaining, and verification.

use std::collections::HashSet;

use serde_json::{Map, Value};
use zeta_substrate::{derive, hash_bytes, CanonicalJsonError, Domain, Hash};

use crate::error::{AppendError, VerificationError, VerificationErrorKind, VerificationReport};
use crate::event::Event;

/// Encodes an event payload with the shared canonical JSON rules.
///
/// # Examples
///
/// ```
/// let payload = serde_json::from_value(serde_json::json!({"z": 2, "a": 1})).unwrap();
/// assert_eq!(
///     zeta_journal::canonical_payload(&payload).unwrap(),
///     br#"{"a":1,"z":2}"#,
/// );
/// ```
///
/// # Errors
///
/// Returns [`CanonicalJsonError`] when a number falls outside the canonical
/// identity value domain.
pub fn canonical_payload(payload: &Map<String, Value>) -> Result<Vec<u8>, CanonicalJsonError> {
    zeta_substrate::canonical_json(&Value::Object(payload.clone()))
}

/// Returns the domainless content address of exact payload bytes.
///
/// # Examples
///
/// ```
/// let address = zeta_journal::payload_address(br#"{"value":1}"#);
/// assert_eq!(address, zeta_substrate::hash_bytes(br#"{"value":1}"#));
/// ```
pub fn payload_address(payload_bytes: &[u8]) -> Hash {
    hash_bytes(payload_bytes)
}

/// Encodes the version-0 journal chain input in its exact positional order.
///
/// # Examples
///
/// ```
/// let event = zeta_journal::Event {
///     id: "evt_example".to_owned(),
///     event_type: "example.created".to_owned(),
///     source: "example".to_owned(),
///     payload: serde_json::Map::new(),
///     idempotency_key: None,
///     caused_by: None,
///     session_id: None,
///     run_id: None,
///     turn_id: None,
///     timestamp_ms: 1,
///     cursor: None,
/// };
/// let payload_address = zeta_substrate::hash_bytes(b"{}");
/// let bytes = zeta_journal::canonical_chain_bytes(&event, &payload_address, None).unwrap();
/// assert!(bytes.starts_with(b"[0,"));
/// ```
///
/// # Errors
///
/// Returns [`AppendError`] when a required event identity field is empty.
pub fn canonical_chain_bytes(
    event: &Event,
    payload_address: &Hash,
    previous_address: Option<&Hash>,
) -> Result<Vec<u8>, AppendError> {
    validate_identity_fields(event)?;
    let Event {
        id,
        event_type,
        source,
        payload: _payload,
        idempotency_key,
        caused_by,
        session_id,
        run_id,
        turn_id,
        timestamp_ms,
        cursor: _cursor,
    } = event;
    let value = vec![
        Value::from(0),
        Value::String(id.clone()),
        Value::String(event_type.clone()),
        Value::String(source.clone()),
        Value::String(payload_address.to_string()),
        optional_text(idempotency_key),
        optional_text(caused_by),
        optional_text(session_id),
        optional_text(run_id),
        optional_text(turn_id),
        Value::from(*timestamp_ms),
        optional_hash(previous_address),
    ];
    zeta_substrate::canonical_json(&Value::Array(value)).map_err(AppendError::from)
}

/// Returns the Chain-domain address of one canonical journal entry.
///
/// # Examples
///
/// ```
/// let event = zeta_journal::Event {
///     id: "evt_example".to_owned(),
///     event_type: "example.created".to_owned(),
///     source: "example".to_owned(),
///     payload: serde_json::Map::new(),
///     idempotency_key: None,
///     caused_by: None,
///     session_id: None,
///     run_id: None,
///     turn_id: None,
///     timestamp_ms: 1,
///     cursor: None,
/// };
/// let payload_address = zeta_substrate::hash_bytes(b"{}");
/// let address = zeta_journal::entry_address(&event, &payload_address, None).unwrap();
/// assert!(address.to_string().starts_with("b3:"));
/// ```
///
/// # Errors
///
/// Returns [`AppendError`] when a required event identity field is empty.
pub fn entry_address(
    event: &Event,
    payload_address: &Hash,
    previous_address: Option<&Hash>,
) -> Result<Hash, AppendError> {
    let bytes = canonical_chain_bytes(event, payload_address, previous_address)?;
    Ok(derive(Domain::Chain, &bytes))
}

/// Pairs one durable event with its backend-independent identity proof.
///
/// # Examples
///
/// ```
/// let event = zeta_journal::Event {
///     id: "evt_example".to_owned(),
///     event_type: "example.created".to_owned(),
///     source: "example".to_owned(),
///     payload: serde_json::Map::new(),
///     idempotency_key: None,
///     caused_by: None,
///     session_id: None,
///     run_id: None,
///     turn_id: None,
///     timestamp_ms: 1,
///     cursor: None,
/// };
/// let entry = zeta_journal::JournalEntry::new(event, 1, None).unwrap();
/// assert_eq!(entry.event.cursor, Some(1));
/// ```
#[derive(Clone, Debug, PartialEq)]
pub struct JournalEntry {
    /// Carries the durable caller-visible event.
    pub event: Event,
    /// Preserves the exact canonical payload bytes.
    pub payload_bytes: Vec<u8>,
    /// Identifies the canonical payload bytes.
    pub payload_address: Hash,
    /// Names the preceding successful journal entry.
    pub previous_address: Option<Hash>,
    /// Identifies this entry's canonical chain input.
    pub entry_address: Hash,
}

impl JournalEntry {
    /// Creates one complete entry and assigns its positive cursor.
    ///
    /// # Examples
    ///
    /// ```
    /// let event = zeta_journal::Event {
    ///     id: "evt_example".to_owned(),
    ///     event_type: "example.created".to_owned(),
    ///     source: "example".to_owned(),
    ///     payload: serde_json::Map::new(),
    ///     idempotency_key: None,
    ///     caused_by: None,
    ///     session_id: None,
    ///     run_id: None,
    ///     turn_id: None,
    ///     timestamp_ms: 1,
    ///     cursor: None,
    /// };
    /// let entry = zeta_journal::JournalEntry::new(event, 1, None).unwrap();
    /// assert_eq!(entry.previous_address, None);
    /// ```
    ///
    /// # Errors
    ///
    /// Returns [`AppendError`] when the cursor is zero, a required event field
    /// is empty, or the payload has no canonical identity encoding.
    pub fn new(
        mut event: Event,
        cursor: u64,
        previous_address: Option<Hash>,
    ) -> Result<Self, AppendError> {
        if cursor == 0 {
            return Err(AppendError::CursorZero);
        }
        validate_identity_fields(&event)?;
        let payload_bytes = canonical_payload(&event.payload)?;
        let payload_address = payload_address(&payload_bytes);
        event.cursor = Some(cursor);
        let entry_address = entry_address(&event, &payload_address, previous_address.as_ref())?;
        Ok(JournalEntry {
            event,
            payload_bytes,
            payload_address,
            previous_address,
            entry_address,
        })
    }
}

/// Selects unanchored verification or an exact trusted head.
///
/// `Exact(None)` anchors an empty journal and therefore differs from
/// `Unanchored`.
///
/// # Examples
///
/// ```
/// let expectation = zeta_journal::HeadExpectation::Exact(None);
/// let entries: Vec<zeta_journal::JournalEntry> = Vec::new();
/// assert!(zeta_journal::verify(&entries, expectation).is_ok());
/// ```
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HeadExpectation<'a> {
    /// Checks only the retained sequence's internal consistency.
    Unanchored,
    /// Requires the retained head to equal the supplied trusted value.
    Exact(Option<&'a Hash>),
}

/// Verifies an ordered retained journal and its optional external anchor.
///
/// # Examples
///
/// ```
/// let entries: Vec<zeta_journal::JournalEntry> = Vec::new();
/// let report = zeta_journal::verify(
///     &entries,
///     zeta_journal::HeadExpectation::Unanchored,
/// )
/// .unwrap();
/// assert_eq!(report.entries_checked, 0);
/// ```
///
/// # Errors
///
/// Returns [`VerificationError`] at the first cursor, uniqueness, payload,
/// linkage, entry-address, or expected-head divergence.
pub fn verify(
    entries: &[JournalEntry],
    expectation: HeadExpectation<'_>,
) -> Result<VerificationReport, VerificationError> {
    let mut previous_address = None;
    let mut previous_cursor = None;
    let mut seen_ids = HashSet::new();
    let mut seen_idempotency_keys = HashSet::new();
    let mut entries_checked = 0;
    let mut last_event_id = None;
    let mut last_cursor = None;

    for entry in entries {
        let JournalEntry {
            event,
            payload_bytes,
            payload_address,
            previous_address: stored_previous_address,
            entry_address: stored_entry_address,
        } = entry;
        let Event {
            id,
            event_type: _event_type,
            source: _source,
            payload,
            idempotency_key,
            caused_by: _caused_by,
            session_id: _session_id,
            run_id: _run_id,
            turn_id: _turn_id,
            timestamp_ms: _timestamp_ms,
            cursor,
        } = event;
        let cursor = *cursor;
        let cursor_is_valid = match cursor {
            Some(cursor) => {
                cursor > 0
                    && match previous_cursor {
                        Some(previous_cursor) => cursor > previous_cursor,
                        None => true,
                    }
            }
            None => false,
        };
        if !cursor_is_valid {
            return Err(verification_error(
                entries_checked,
                id,
                cursor,
                VerificationErrorKind::CursorOrder {
                    previous: previous_cursor,
                    actual: cursor,
                },
            ));
        }
        if !seen_ids.insert(id.clone()) {
            return Err(verification_error(
                entries_checked,
                id,
                cursor,
                VerificationErrorKind::DuplicateId,
            ));
        }
        match idempotency_key {
            Some(idempotency_key) if seen_idempotency_keys.insert(idempotency_key.clone()) => {}
            Some(idempotency_key) => {
                return Err(verification_error(
                    entries_checked,
                    id,
                    cursor,
                    VerificationErrorKind::DuplicateIdempotencyKey {
                        key: idempotency_key.clone(),
                    },
                ));
            }
            None => {}
        }
        let payload = canonical_payload(payload);
        let Ok(payload) = payload else {
            return Err(verification_error(
                entries_checked,
                id,
                cursor,
                VerificationErrorKind::PayloadEncoding,
            ));
        };
        if payload_bytes != &payload {
            return Err(verification_error(
                entries_checked,
                id,
                cursor,
                VerificationErrorKind::PayloadEncoding,
            ));
        }
        let expected_payload_address = crate::chain::payload_address(&payload);
        if payload_address != &expected_payload_address {
            return Err(verification_error(
                entries_checked,
                id,
                cursor,
                VerificationErrorKind::PayloadAddressMismatch {
                    expected: expected_payload_address,
                    actual: *payload_address,
                },
            ));
        }
        if stored_previous_address != &previous_address {
            return Err(verification_error(
                entries_checked,
                id,
                cursor,
                VerificationErrorKind::PreviousAddressMismatch {
                    expected: previous_address,
                    actual: *stored_previous_address,
                },
            ));
        }
        let expected_entry_address =
            entry_address(event, &expected_payload_address, previous_address.as_ref());
        let expected_entry_address = expected_entry_address.ok();
        if expected_entry_address != Some(*stored_entry_address) {
            return Err(verification_error(
                entries_checked,
                id,
                cursor,
                VerificationErrorKind::EntryAddressMismatch {
                    expected: expected_entry_address,
                    actual: *stored_entry_address,
                },
            ));
        }

        entries_checked += 1;
        previous_cursor = cursor;
        previous_address = expected_entry_address;
        last_event_id = Some(id.clone());
        last_cursor = cursor;
    }

    match expectation {
        HeadExpectation::Unanchored => {}
        HeadExpectation::Exact(expected) => {
            let expected = expected.copied();
            if previous_address != expected {
                return Err(VerificationError {
                    entries_checked,
                    event_id: last_event_id,
                    cursor: last_cursor,
                    kind: VerificationErrorKind::ExpectedHeadMismatch {
                        expected,
                        actual: previous_address,
                    },
                });
            }
        }
    }
    Ok(VerificationReport {
        entries_checked,
        head: previous_address,
    })
}

pub(crate) fn validate_identity_fields(event: &Event) -> Result<(), AppendError> {
    let Event {
        id,
        event_type,
        source,
        payload: _payload,
        idempotency_key: _idempotency_key,
        caused_by: _caused_by,
        session_id: _session_id,
        run_id: _run_id,
        turn_id: _turn_id,
        timestamp_ms: _timestamp_ms,
        cursor: _cursor,
    } = event;
    if id.is_empty() {
        return Err(AppendError::EmptyId);
    }
    if event_type.is_empty() {
        return Err(AppendError::EmptyEventType);
    }
    if source.is_empty() {
        return Err(AppendError::EmptySource);
    }
    Ok(())
}

fn optional_text(value: &Option<String>) -> Value {
    match value {
        Some(value) => Value::String(value.clone()),
        None => Value::Null,
    }
}

fn optional_hash(value: Option<&Hash>) -> Value {
    match value {
        Some(value) => Value::String(value.to_string()),
        None => Value::Null,
    }
}

fn verification_error(
    entries_checked: usize,
    event_id: &str,
    cursor: Option<u64>,
    kind: VerificationErrorKind,
) -> VerificationError {
    VerificationError {
        entries_checked,
        event_id: Some(event_id.to_owned()),
        cursor,
        kind,
    }
}
