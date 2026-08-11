//! Event values, append outcomes, and backend-independent query filters.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

/// Describes one event before its durable identity and time are assigned.
///
/// # Examples
///
/// ```
/// let draft = zeta::journal::DraftEvent {
///     event_type: "example.created".to_owned(),
///     source: "example".to_owned(),
///     payload: serde_json::Map::new(),
///     idempotency_key: None,
///     caused_by: None,
///     session_id: None,
///     run_id: None,
///     turn_id: None,
/// };
/// assert_eq!(draft.event_type, "example.created");
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct DraftEvent {
    /// Names the event vocabulary entry.
    #[serde(rename = "type")]
    pub event_type: String,
    /// Names the producer.
    pub source: String,
    /// Carries the event-specific JSON object.
    pub payload: Map<String, Value>,
    /// Identifies a retry of the same logical append.
    pub idempotency_key: Option<String>,
    /// Names the causal parent event.
    pub caused_by: Option<String>,
    /// Associates the event with a session.
    pub session_id: Option<String>,
    /// Associates the event with a run.
    pub run_id: Option<String>,
    /// Associates the event with a turn.
    pub turn_id: Option<String>,
}

/// Stores one caller-visible durable event.
///
/// The caller supplies `id` and `timestamp_ms`. A journal assigns `cursor`
/// when the event is inserted; an event awaiting append carries `None`.
///
/// # Examples
///
/// ```
/// let draft = zeta::journal::DraftEvent {
///     event_type: "example.created".to_owned(),
///     source: "example".to_owned(),
///     payload: serde_json::Map::new(),
///     idempotency_key: None,
///     caused_by: None,
///     session_id: None,
///     run_id: None,
///     turn_id: None,
/// };
/// let event = zeta::journal::Event::from_draft("evt_example", 1, draft);
/// assert_eq!(event.cursor, None);
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct Event {
    /// Carries the opaque event identity.
    pub id: String,
    /// Names the event vocabulary entry.
    #[serde(rename = "type")]
    pub event_type: String,
    /// Names the producer.
    pub source: String,
    /// Carries the event-specific JSON object.
    pub payload: Map<String, Value>,
    /// Identifies a retry of the same logical append.
    pub idempotency_key: Option<String>,
    /// Names the causal parent event.
    pub caused_by: Option<String>,
    /// Associates the event with a session.
    pub session_id: Option<String>,
    /// Associates the event with a run.
    pub run_id: Option<String>,
    /// Associates the event with a turn.
    pub turn_id: Option<String>,
    /// Records the producer time in Unix milliseconds.
    pub timestamp_ms: i64,
    /// Orders successful appends independently of producer time.
    #[serde(default)]
    pub cursor: Option<u64>,
}

impl Event {
    /// Creates an event with caller-supplied identity and time.
    ///
    /// Leading and trailing whitespace is removed from an idempotency key;
    /// an empty result becomes `None`.
    ///
    /// # Examples
    ///
    /// ```
    /// let draft = zeta::journal::DraftEvent {
    ///     event_type: "example.created".to_owned(),
    ///     source: "example".to_owned(),
    ///     payload: serde_json::Map::new(),
    ///     idempotency_key: Some(" retry:1 ".to_owned()),
    ///     caused_by: None,
    ///     session_id: None,
    ///     run_id: None,
    ///     turn_id: None,
    /// };
    /// let event = zeta::journal::Event::from_draft("evt_example", 42, draft);
    /// assert_eq!(event.idempotency_key.as_deref(), Some("retry:1"));
    /// ```
    pub fn from_draft(id: &str, timestamp_ms: i64, draft: DraftEvent) -> Self {
        let DraftEvent {
            event_type,
            source,
            payload,
            idempotency_key,
            caused_by,
            session_id,
            run_id,
            turn_id,
        } = draft;
        let idempotency_key = match idempotency_key {
            Some(idempotency_key) => {
                let idempotency_key = idempotency_key.trim();
                if idempotency_key.is_empty() {
                    None
                } else {
                    Some(idempotency_key.to_owned())
                }
            }
            None => None,
        };
        Event {
            id: id.to_owned(),
            event_type,
            source,
            payload,
            idempotency_key,
            caused_by,
            session_id,
            run_id,
            turn_id,
            timestamp_ms,
            cursor: None,
        }
    }
}

/// Reports whether append inserted a new event or resolved a duplicate.
///
/// # Examples
///
/// ```
/// let mut journal = zeta::journal::MemoryJournal::new();
/// let event = zeta::journal::Event {
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
/// let outcome = journal.append(event).unwrap();
/// assert!(outcome.inserted);
/// ```
#[derive(Clone, Debug, PartialEq)]
pub struct AppendOutcome {
    /// Returns the inserted event or the previously stored duplicate.
    pub event: Event,
    /// Reports whether the journal grew.
    pub inserted: bool,
}

/// Selects a cursor-ordered logical event slice.
///
/// All populated fields combine with logical AND. `limit` is unsigned, so a
/// negative limit cannot be represented by the Rust API.
///
/// # Examples
///
/// ```
/// let filter = zeta::journal::Filter {
///     event_type_prefix: Some("example.".to_owned()),
///     limit: Some(10),
///     ..zeta::journal::Filter::default()
/// };
/// assert_eq!(filter.limit, Some(10));
/// ```
#[derive(Clone, Debug, Default, Deserialize, PartialEq, Eq, Serialize)]
#[serde(default)]
pub struct Filter {
    /// Matches an exact event type.
    pub event_type: Option<String>,
    /// Matches a literal, case-sensitive event-type prefix.
    pub event_type_prefix: Option<String>,
    /// Matches an exact session association.
    pub session_id: Option<String>,
    /// Matches an exact run association.
    pub run_id: Option<String>,
    /// Matches an exact turn association.
    pub turn_id: Option<String>,
    /// Matches an exact causal parent.
    pub caused_by: Option<String>,
    /// Keeps events whose cursor is strictly greater than this value.
    pub after_cursor: Option<u64>,
    /// Bounds the result after ordering.
    pub limit: Option<usize>,
    /// Reverses the default cursor-ascending order.
    pub newest_first: bool,
}
