use std::collections::HashSet;

use rusqlite::{params, Connection, OptionalExtension, Row, Transaction, TransactionBehavior};
use serde_json::{Map, Value};
use zeta_journal::{
    canonical_payload, payload_address, verify, AppendError, AppendOutcome, Event, EventFilter,
    HeadExpectation, JournalEntry, VerificationReport,
};
use zeta_substrate::Hash;

use super::projection::index_event;
use super::{database_error, Dispatch, DispatchError};
use crate::dispatch::RuntimeEventIdentity;

const ENTRY_COLUMNS: &str = "cursor, event_id, event_type, source, payload_bytes, \
    payload_address, idempotency_key, caused_by, session_id, run_id, turn_id, \
    timestamp_ms, previous_address, entry_address";

impl Dispatch {
    /// Appends an event or resolves its id-first duplicate atomically.
    ///
    /// An id duplicate must match the retained payload address; a divergent
    /// candidate fails instead of silently resolving. An idempotency-key
    /// duplicate is never compared, matching journal-v0 retry semantics.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] for invalid new events, storage failures, or
    /// corrupted retained journal rows.
    pub fn append_trusted_event(&mut self, event: Event) -> Result<AppendOutcome, DispatchError> {
        validate_event_identity(&event)?;
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| database_error("begin journal append", error))?;
        let outcome = append_in_transaction(&transaction, event)?;
        if outcome.inserted {
            index_event(&transaction, &outcome.event)?;
        }
        transaction
            .commit()
            .map_err(|error| database_error("commit journal append", error))?;
        Ok(outcome)
    }

    /// Accepts one externally authored event and creates its pending work item.
    ///
    /// Journal insertion and queue projection share one immediate transaction.
    /// An id or idempotency-key duplicate returns the retained event without
    /// creating duplicate work.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError::ReservedRuntimeEvent`] for `runtime.*` input,
    /// or another [`DispatchError`] when append or projection fails.
    pub fn ingest_event(&mut self, event: Event) -> Result<AppendOutcome, DispatchError> {
        if event.event_type.starts_with("runtime.") {
            return Err(DispatchError::ReservedRuntimeEvent {
                event_type: event.event_type,
            });
        }
        self.append_trusted_event(event)
    }

    /// Returns the final retained entry address, or `None` for an empty journal.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when the last entry cannot be read or its
    /// stored address is malformed.
    pub fn head(&self) -> Result<Option<Hash>, DispatchError> {
        let anchor = last_entry_anchor(&self.connection)?;
        Ok(anchor.map(|(_cursor, address)| address))
    }

    /// Returns one exact durable event by opaque id.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when the row cannot be read as a complete
    /// journal-v0 entry.
    pub fn get_event(&self, event_id: &str) -> Result<Option<Event>, DispatchError> {
        let entry = entry_by_field(&self.connection, "event_id", event_id)?;
        Ok(entry.map(|entry| entry.event))
    }

    /// Returns cursor-ordered events matching every populated filter field.
    ///
    /// Literal prefix matching runs in Rust so SQLite wildcard and collation
    /// rules cannot alter journal-v0 behavior.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when any retained entry is malformed or a
    /// database read fails.
    pub fn list_events(&self, filter: &EventFilter) -> Result<Vec<Event>, DispatchError> {
        if filter.limit == Some(0) {
            return Ok(Vec::new());
        }
        let entries = load_entries(&self.connection, filter.newest_first)?;
        let mut events = Vec::new();
        for entry in entries {
            if !event_matches(&entry.event, filter) {
                continue;
            }
            events.push(entry.event);
            if filter.limit == Some(events.len()) {
                break;
            }
        }
        Ok(events)
    }

    /// Returns cursor-ordered direct causal children.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when retained entries cannot be read.
    pub fn children(
        &self,
        event_id: &str,
        limit: Option<usize>,
    ) -> Result<Vec<Event>, DispatchError> {
        let filter = EventFilter {
            caused_by: Some(event_id.to_owned()),
            limit,
            ..EventFilter::default()
        };
        self.list_events(&filter)
    }

    /// Returns the oldest reachable causal ancestor through one event.
    ///
    /// Missing parents and repeated ids terminate traversal because they are
    /// valid application metadata, not journal-chain corruption.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when a retained entry cannot be read.
    pub fn causal_chain(&self, event_id: &str) -> Result<Vec<Event>, DispatchError> {
        let mut chain = Vec::new();
        let mut seen = HashSet::new();
        let mut current = self.get_event(event_id)?;
        while let Some(event) = current {
            if !seen.insert(event.id.clone()) {
                break;
            }
            let caused_by = event.caused_by.clone();
            chain.push(event);
            let Some(caused_by) = caused_by else {
                break;
            };
            current = self.get_event(&caused_by)?;
        }
        chain.reverse();
        Ok(chain)
    }

    /// Reconstructs and verifies every retained journal-v0 proof value.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] for unreadable stored proof fields or the
    /// first semantic divergence reported by `zeta-journal`.
    pub fn verify_journal(
        &self,
        expectation: HeadExpectation<'_>,
    ) -> Result<VerificationReport, DispatchError> {
        let entries = load_entries(&self.connection, false)?;
        verify(&entries, expectation).map_err(DispatchError::Verification)
    }
}

pub(super) fn same_logical_event(candidate: &Event, retained: &Event) -> bool {
    let mut retained = retained.clone();
    retained.cursor = candidate.cursor;
    candidate == &retained
}

pub(super) fn same_lifecycle_intention(candidate: &Event, retained: &Event) -> bool {
    candidate.event_type == retained.event_type
        && candidate.source == retained.source
        && candidate.payload == retained.payload
        && candidate.idempotency_key == retained.idempotency_key
        && candidate.caused_by == retained.caused_by
        && candidate.session_id == retained.session_id
        && candidate.run_id == retained.run_id
        && candidate.turn_id == retained.turn_id
}

/// Appends a runtime lifecycle candidate, classifying divergent id
/// duplicates as identity collisions.
///
/// Every lifecycle path already treated a payload difference under a reused
/// id as a collision, so the journal-level mismatch keeps that shape instead
/// of surfacing as a bare append failure.
pub(super) fn append_lifecycle_candidate(
    transaction: &Transaction<'_>,
    event: Event,
) -> Result<AppendOutcome, DispatchError> {
    let event_id = event.id.clone();
    match append_in_transaction(transaction, event) {
        Ok(outcome) => Ok(outcome),
        Err(DispatchError::Append(AppendError::DuplicateIdPayloadMismatch)) => {
            Err(DispatchError::RuntimeEventIdentityCollision { event_id })
        }
        Err(error) => Err(error),
    }
}

pub(super) fn append_runtime_event(
    transaction: &Transaction<'_>,
    event: Event,
) -> Result<AppendOutcome, DispatchError> {
    validate_event_identity(&event)?;
    let candidate = event.clone();
    let outcome = append_lifecycle_candidate(transaction, event)?;
    if !outcome.inserted && !same_lifecycle_intention(&candidate, &outcome.event) {
        return Err(DispatchError::RuntimeEventIdentityCollision {
            event_id: candidate.id,
        });
    }
    if outcome.inserted {
        index_event(transaction, &outcome.event)?;
    }
    Ok(outcome)
}

pub(super) fn validate_distinct_runtime_identities(
    identities: &[RuntimeEventIdentity],
) -> Result<(), DispatchError> {
    let mut seen = HashSet::new();
    for identity in identities {
        if !seen.insert(identity.id()) {
            return Err(DispatchError::DuplicateRuntimeEventIdentity {
                event_id: identity.id().to_owned(),
            });
        }
    }
    Ok(())
}

pub(super) fn validate_event_identity(event: &Event) -> Result<(), DispatchError> {
    if event.id.is_empty() {
        return Err(AppendError::EmptyId.into());
    }
    if event.event_type.is_empty() {
        return Err(AppendError::EmptyEventType.into());
    }
    if event.source.is_empty() {
        return Err(AppendError::EmptySource.into());
    }
    Ok(())
}

pub(super) fn append_in_transaction(
    transaction: &Transaction<'_>,
    event: Event,
) -> Result<AppendOutcome, DispatchError> {
    if let Some(entry) = entry_by_field(transaction, "event_id", &event.id)? {
        let payload = canonical_payload(&event.payload).map_err(AppendError::PayloadEncoding)?;
        if payload_address(&payload) != entry.payload_address {
            return Err(AppendError::DuplicateIdPayloadMismatch.into());
        }
        return Ok(AppendOutcome {
            event: entry.event,
            inserted: false,
        });
    }
    if let Some(idempotency_key) = &event.idempotency_key {
        if let Some(entry) = entry_by_field(transaction, "idempotency_key", idempotency_key)? {
            return Ok(AppendOutcome {
                event: entry.event,
                inserted: false,
            });
        }
    }

    let anchor = last_entry_anchor(transaction)?;
    let (cursor, previous_address) = match anchor {
        Some((cursor, address)) => {
            let Some(cursor) = cursor.checked_add(1) else {
                return Err(AppendError::CursorExhausted.into());
            };
            (cursor, Some(address))
        }
        None => (1, None),
    };
    if cursor > i64::MAX as u64 {
        return Err(AppendError::CursorExhausted.into());
    }
    let entry = JournalEntry::new(event, cursor, previous_address)?;
    let previous_address = entry
        .previous_address
        .map(|address| address.as_bytes().to_vec());
    transaction
        .execute(
            "INSERT INTO journal_entries (
                cursor, event_id, event_type, source, payload_bytes,
                payload_address, idempotency_key, caused_by, session_id,
                run_id, turn_id, timestamp_ms, previous_address, entry_address
             ) VALUES (
                ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14
             )",
            params![
                cursor as i64,
                &entry.event.id,
                &entry.event.event_type,
                &entry.event.source,
                &entry.payload_bytes,
                entry.payload_address.as_bytes().as_slice(),
                entry.event.idempotency_key.as_deref(),
                entry.event.caused_by.as_deref(),
                entry.event.session_id.as_deref(),
                entry.event.run_id.as_deref(),
                entry.event.turn_id.as_deref(),
                entry.event.timestamp_ms,
                previous_address,
                entry.entry_address.as_bytes().as_slice(),
            ],
        )
        .map_err(|error| database_error("insert journal entry", error))?;
    Ok(AppendOutcome {
        event: entry.event,
        inserted: true,
    })
}

pub(super) fn entry_by_field(
    connection: &Connection,
    field: &'static str,
    value: &str,
) -> Result<Option<JournalEntry>, DispatchError> {
    let sql = if field == "event_id" {
        format!("SELECT {ENTRY_COLUMNS} FROM journal_entries WHERE event_id = ?1")
    } else if field == "idempotency_key" {
        format!("SELECT {ENTRY_COLUMNS} FROM journal_entries WHERE idempotency_key = ?1")
    } else {
        return Err(DispatchError::Database {
            operation: "select journal entry",
            message: format!("unsupported lookup field {field:?}"),
        });
    };
    let stored = connection
        .query_row(&sql, params![value], StoredEntry::from_row)
        .optional()
        .map_err(|error| database_error("select journal entry", error))?;
    stored.map(StoredEntry::into_entry).transpose()
}

fn last_entry_anchor(connection: &Connection) -> Result<Option<(u64, Hash)>, DispatchError> {
    let stored = connection
        .query_row(
            "SELECT cursor, entry_address
             FROM journal_entries ORDER BY cursor DESC LIMIT 1",
            [],
            |row| Ok((row.get::<_, i64>(0)?, row.get::<_, Vec<u8>>(1)?)),
        )
        .optional()
        .map_err(|error| database_error("read journal head", error))?;
    let Some((cursor, address)) = stored else {
        return Ok(None);
    };
    let cursor = decode_cursor(cursor)?;
    let address = decode_hash(Some(cursor), "entry_address", address)?;
    Ok(Some((cursor, address)))
}

pub(super) fn load_entries(
    connection: &Connection,
    newest_first: bool,
) -> Result<Vec<JournalEntry>, DispatchError> {
    let order = if newest_first { "DESC" } else { "ASC" };
    let sql = format!("SELECT {ENTRY_COLUMNS} FROM journal_entries ORDER BY cursor {order}");
    let mut statement = connection
        .prepare(&sql)
        .map_err(|error| database_error("prepare journal read", error))?;
    let rows = statement
        .query_map([], StoredEntry::from_row)
        .map_err(|error| database_error("read journal entries", error))?;
    let mut entries = Vec::new();
    for row in rows {
        let stored = row.map_err(|error| database_error("read journal entry", error))?;
        entries.push(stored.into_entry()?);
    }
    Ok(entries)
}

fn event_matches(event: &Event, filter: &EventFilter) -> bool {
    if let Some(expected) = &filter.event_type {
        if &event.event_type != expected {
            return false;
        }
    }
    if let Some(prefix) = &filter.event_type_prefix {
        if !event.event_type.starts_with(prefix) {
            return false;
        }
    }
    if let Some(expected) = &filter.session_id {
        if event.session_id.as_ref() != Some(expected) {
            return false;
        }
    }
    if let Some(expected) = &filter.run_id {
        if event.run_id.as_ref() != Some(expected) {
            return false;
        }
    }
    if let Some(expected) = &filter.turn_id {
        if event.turn_id.as_ref() != Some(expected) {
            return false;
        }
    }
    if let Some(expected) = &filter.caused_by {
        if event.caused_by.as_ref() != Some(expected) {
            return false;
        }
    }
    if let Some(after_cursor) = filter.after_cursor {
        let Some(cursor) = event.cursor else {
            return false;
        };
        if cursor <= after_cursor {
            return false;
        }
    }
    true
}

struct StoredEntry {
    cursor: i64,
    event_id: String,
    event_type: String,
    source: String,
    payload_bytes: Vec<u8>,
    payload_address: Vec<u8>,
    idempotency_key: Option<String>,
    caused_by: Option<String>,
    session_id: Option<String>,
    run_id: Option<String>,
    turn_id: Option<String>,
    timestamp_ms: i64,
    previous_address: Option<Vec<u8>>,
    entry_address: Vec<u8>,
}

impl StoredEntry {
    fn from_row(row: &Row<'_>) -> rusqlite::Result<Self> {
        Ok(StoredEntry {
            cursor: row.get(0)?,
            event_id: row.get(1)?,
            event_type: row.get(2)?,
            source: row.get(3)?,
            payload_bytes: row.get(4)?,
            payload_address: row.get(5)?,
            idempotency_key: row.get(6)?,
            caused_by: row.get(7)?,
            session_id: row.get(8)?,
            run_id: row.get(9)?,
            turn_id: row.get(10)?,
            timestamp_ms: row.get(11)?,
            previous_address: row.get(12)?,
            entry_address: row.get(13)?,
        })
    }

    fn into_entry(self) -> Result<JournalEntry, DispatchError> {
        let cursor = decode_cursor(self.cursor)?;
        let payload: Map<String, Value> =
            serde_json::from_slice(&self.payload_bytes).map_err(|_error| {
                DispatchError::CorruptJournal {
                    cursor: Some(cursor),
                    field: "payload_bytes",
                }
            })?;
        let payload_address = decode_hash(Some(cursor), "payload_address", self.payload_address)?;
        let previous_address = self
            .previous_address
            .map(|address| decode_hash(Some(cursor), "previous_address", address))
            .transpose()?;
        let entry_address = decode_hash(Some(cursor), "entry_address", self.entry_address)?;
        Ok(JournalEntry {
            event: Event {
                id: self.event_id,
                event_type: self.event_type,
                source: self.source,
                payload,
                idempotency_key: self.idempotency_key,
                caused_by: self.caused_by,
                session_id: self.session_id,
                run_id: self.run_id,
                turn_id: self.turn_id,
                timestamp_ms: self.timestamp_ms,
                cursor: Some(cursor),
            },
            payload_bytes: self.payload_bytes,
            payload_address,
            previous_address,
            entry_address,
        })
    }
}

fn decode_cursor(cursor: i64) -> Result<u64, DispatchError> {
    if cursor <= 0 {
        return Err(DispatchError::CorruptJournal {
            cursor: None,
            field: "cursor",
        });
    }
    Ok(cursor as u64)
}

fn decode_hash(
    cursor: Option<u64>,
    field: &'static str,
    bytes: Vec<u8>,
) -> Result<Hash, DispatchError> {
    let bytes: Result<[u8; 32], Vec<u8>> = bytes.try_into();
    let Ok(bytes) = bytes else {
        return Err(DispatchError::CorruptJournal { cursor, field });
    };
    Ok(Hash::from_bytes(bytes))
}
