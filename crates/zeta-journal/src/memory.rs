//! Deterministic in-memory reference behavior for journal-v0.

use std::collections::{HashMap, HashSet};

use zeta_substrate::Hash;

use crate::chain::{validate_identity_fields, JournalEntry};
use crate::error::AppendError;
use crate::event::{AppendOutcome, Event, Filter};

/// Stores complete journal entries in process memory.
///
/// This is a conformance reference, not a persistence abstraction. Dispatch
/// will define an adapter when it chooses a durable storage backend.
///
/// # Examples
///
/// ```
/// let journal = zeta_journal::MemoryJournal::new();
/// assert!(journal.entries().is_empty());
/// assert_eq!(journal.head(), None);
/// ```
#[derive(Debug)]
pub struct MemoryJournal {
    entries: Vec<JournalEntry>,
    by_id: HashMap<String, usize>,
    by_idempotency_key: HashMap<String, usize>,
    next_cursor: u64,
    head: Option<Hash>,
}

impl MemoryJournal {
    /// Creates an empty journal whose first inserted cursor is one.
    ///
    /// # Examples
    ///
    /// ```
    /// let journal = zeta_journal::MemoryJournal::new();
    /// assert_eq!(journal.head(), None);
    /// ```
    pub fn new() -> Self {
        MemoryJournal {
            entries: Vec::new(),
            by_id: HashMap::new(),
            by_idempotency_key: HashMap::new(),
            next_cursor: 1,
            head: None,
        }
    }

    /// Appends a new event or returns the existing id-first duplicate.
    ///
    /// Required identity fields are validated before duplicate resolution.
    /// Payload encoding is validated only for a new event because duplicate
    /// candidate content has no effect on the retained journal.
    ///
    /// # Examples
    ///
    /// ```
    /// let mut journal = zeta_journal::MemoryJournal::new();
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
    /// assert!(journal.append(event).unwrap().inserted);
    /// ```
    ///
    /// # Errors
    ///
    /// Returns [`AppendError`] when a required identity field is empty, the
    /// new payload is not canonicalizable, or the cursor range is exhausted.
    pub fn append(&mut self, event: Event) -> Result<AppendOutcome, AppendError> {
        validate_identity_fields(&event)?;
        let Event {
            id,
            event_type: _event_type,
            source: _source,
            payload: _payload,
            idempotency_key,
            caused_by: _caused_by,
            session_id: _session_id,
            run_id: _run_id,
            turn_id: _turn_id,
            timestamp_ms: _timestamp_ms,
            cursor: _cursor,
        } = &event;
        let duplicate_index = match self.by_id.get(id) {
            Some(duplicate_index) => Some(*duplicate_index),
            None => match idempotency_key {
                Some(idempotency_key) => self.by_idempotency_key.get(idempotency_key).copied(),
                None => None,
            },
        };
        if let Some(duplicate_index) = duplicate_index {
            let duplicate = &self.entries[duplicate_index];
            return Ok(AppendOutcome {
                event: duplicate.event.clone(),
                inserted: false,
            });
        }

        let cursor = self.next_cursor;
        let Some(next_cursor) = cursor.checked_add(1) else {
            return Err(AppendError::CursorExhausted);
        };
        let entry = JournalEntry::new(event, cursor, self.head)?;
        let Event {
            id,
            event_type: _event_type,
            source: _source,
            payload: _payload,
            idempotency_key,
            caused_by: _caused_by,
            session_id: _session_id,
            run_id: _run_id,
            turn_id: _turn_id,
            timestamp_ms: _timestamp_ms,
            cursor: _cursor,
        } = &entry.event;
        let index = self.entries.len();
        self.by_id.insert(id.clone(), index);
        if let Some(idempotency_key) = idempotency_key {
            self.by_idempotency_key
                .insert(idempotency_key.clone(), index);
        }
        self.next_cursor = next_cursor;
        self.head = Some(entry.entry_address);
        let event = entry.event.clone();
        self.entries.push(entry);
        Ok(AppendOutcome {
            event,
            inserted: true,
        })
    }

    /// Returns the retained entries in cursor order.
    ///
    /// # Examples
    ///
    /// ```
    /// let journal = zeta_journal::MemoryJournal::new();
    /// assert_eq!(journal.entries().len(), 0);
    /// ```
    pub fn entries(&self) -> &[JournalEntry] {
        &self.entries
    }

    /// Returns the latest successful entry address.
    ///
    /// # Examples
    ///
    /// ```
    /// let journal = zeta_journal::MemoryJournal::new();
    /// assert_eq!(journal.head(), None);
    /// ```
    pub fn head(&self) -> Option<Hash> {
        self.head
    }

    /// Returns one event by opaque id.
    ///
    /// # Examples
    ///
    /// ```
    /// let journal = zeta_journal::MemoryJournal::new();
    /// assert_eq!(journal.get("evt_missing"), None);
    /// ```
    pub fn get(&self, event_id: &str) -> Option<&Event> {
        let index = self.by_id.get(event_id)?;
        Some(&self.entries[*index].event)
    }

    /// Returns events matching every populated filter field.
    ///
    /// # Examples
    ///
    /// ```
    /// let journal = zeta_journal::MemoryJournal::new();
    /// let events = journal.list_events(&zeta_journal::Filter::default());
    /// assert!(events.is_empty());
    /// ```
    pub fn list_events(&self, filter: &Filter) -> Vec<&Event> {
        let mut events = Vec::new();
        if filter.newest_first {
            for entry in self.entries.iter().rev() {
                let JournalEntry {
                    event,
                    payload_bytes: _payload_bytes,
                    payload_address: _payload_address,
                    previous_address: _previous_address,
                    entry_address: _entry_address,
                } = entry;
                if event_matches(event, filter) {
                    events.push(event);
                }
            }
        } else {
            for entry in &self.entries {
                let JournalEntry {
                    event,
                    payload_bytes: _payload_bytes,
                    payload_address: _payload_address,
                    previous_address: _previous_address,
                    entry_address: _entry_address,
                } = entry;
                if event_matches(event, filter) {
                    events.push(event);
                }
            }
        }
        if let Some(limit) = filter.limit {
            events.truncate(limit);
        }
        events
    }

    /// Returns cursor-ordered direct causal children.
    ///
    /// # Examples
    ///
    /// ```
    /// let journal = zeta_journal::MemoryJournal::new();
    /// assert!(journal.children("evt_parent", None).is_empty());
    /// ```
    pub fn children(&self, event_id: &str, limit: Option<usize>) -> Vec<&Event> {
        let filter = Filter {
            caused_by: Some(event_id.to_owned()),
            limit,
            ..Filter::default()
        };
        self.list_events(&filter)
    }

    /// Returns the oldest reachable causal ancestor through the target event.
    ///
    /// Traversal stops at a null parent, missing parent, or repeated id.
    ///
    /// # Examples
    ///
    /// ```
    /// let journal = zeta_journal::MemoryJournal::new();
    /// assert!(journal.causal_chain("evt_missing").is_empty());
    /// ```
    pub fn causal_chain(&self, event_id: &str) -> Vec<&Event> {
        let mut chain = Vec::new();
        let mut seen = HashSet::new();
        let mut current = self.get(event_id);
        loop {
            let Some(event) = current else {
                break;
            };
            if !seen.insert(event.id.clone()) {
                break;
            }
            chain.push(event);
            let Some(caused_by) = &event.caused_by else {
                break;
            };
            current = self.get(caused_by);
        }
        chain.reverse();
        chain
    }

    /// Returns cursor-ordered events associated with one turn.
    ///
    /// # Examples
    ///
    /// ```
    /// let journal = zeta_journal::MemoryJournal::new();
    /// assert!(journal.events_for_turn("turn_1").is_empty());
    /// ```
    pub fn events_for_turn(&self, turn_id: &str) -> Vec<&Event> {
        let filter = Filter {
            turn_id: Some(turn_id.to_owned()),
            ..Filter::default()
        };
        self.list_events(&filter)
    }

    /// Returns cursor-ordered events associated with one run.
    ///
    /// # Examples
    ///
    /// ```
    /// let journal = zeta_journal::MemoryJournal::new();
    /// assert!(journal.events_for_run("run_1").is_empty());
    /// ```
    pub fn events_for_run(&self, run_id: &str) -> Vec<&Event> {
        let filter = Filter {
            run_id: Some(run_id.to_owned()),
            ..Filter::default()
        };
        self.list_events(&filter)
    }
}

impl Default for MemoryJournal {
    fn default() -> Self {
        Self::new()
    }
}

fn event_matches(event: &Event, filter: &Filter) -> bool {
    let Event {
        id: _id,
        event_type,
        source: _source,
        payload: _payload,
        idempotency_key: _idempotency_key,
        caused_by,
        session_id,
        run_id,
        turn_id,
        timestamp_ms: _timestamp_ms,
        cursor,
    } = event;
    let Filter {
        event_type: expected_event_type,
        event_type_prefix,
        session_id: expected_session_id,
        run_id: expected_run_id,
        turn_id: expected_turn_id,
        caused_by: expected_caused_by,
        after_cursor,
        limit: _limit,
        newest_first: _newest_first,
    } = filter;
    match expected_event_type {
        Some(expected_event_type) if event_type != expected_event_type => return false,
        Some(_matching_event_type) => {}
        None => {}
    }
    match event_type_prefix {
        Some(event_type_prefix) if !event_type.starts_with(event_type_prefix) => return false,
        Some(_matching_prefix) => {}
        None => {}
    }
    match expected_session_id {
        Some(expected_session_id) if session_id.as_ref() != Some(expected_session_id) => {
            return false;
        }
        Some(_matching_session_id) => {}
        None => {}
    }
    match expected_run_id {
        Some(expected_run_id) if run_id.as_ref() != Some(expected_run_id) => return false,
        Some(_matching_run_id) => {}
        None => {}
    }
    match expected_turn_id {
        Some(expected_turn_id) if turn_id.as_ref() != Some(expected_turn_id) => return false,
        Some(_matching_turn_id) => {}
        None => {}
    }
    match expected_caused_by {
        Some(expected_caused_by) if caused_by.as_ref() != Some(expected_caused_by) => {
            return false;
        }
        Some(_matching_caused_by) => {}
        None => {}
    }
    if let Some(after_cursor) = after_cursor {
        let Some(cursor) = cursor else {
            return false;
        };
        if cursor <= after_cursor {
            return false;
        }
    }
    true
}
