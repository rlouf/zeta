//! Unit behavior for journal values, chain coverage, and verification.

use serde_json::{json, Map, Value};
use zeta_journal::{
    canonical_payload, entry_address, payload_address, verify, AppendError, DraftEvent, Event,
    HeadExpectation, JournalEntry, MemoryJournal, VerificationError, VerificationErrorKind,
};
use zeta_substrate::Hash;

fn fields(value: Value) -> Map<String, Value> {
    value.as_object().unwrap().clone()
}

fn event(id: &str) -> Event {
    Event {
        id: id.to_owned(),
        event_type: "example.created".to_owned(),
        source: "unit".to_owned(),
        payload: fields(json!({"value": 1})),
        idempotency_key: Some("example:1".to_owned()),
        caused_by: None,
        session_id: Some("session/1".to_owned()),
        run_id: Some("run_1".to_owned()),
        turn_id: Some("turn_1".to_owned()),
        timestamp_ms: 1_786_422_000_000,
        cursor: None,
    }
}

fn address_for(event: &Event, previous: Option<&Hash>) -> Hash {
    let payload = canonical_payload(&event.payload).unwrap();
    let payload_address = payload_address(&payload);
    entry_address(event, &payload_address, previous).unwrap()
}

#[test]
fn event_from_draft_uses_caller_supplied_identity_and_time() {
    let draft = DraftEvent {
        event_type: "example.created".to_owned(),
        source: "unit".to_owned(),
        payload: fields(json!({"value": true})),
        idempotency_key: Some("  example:1  ".to_owned()),
        caused_by: None,
        session_id: None,
        run_id: None,
        turn_id: None,
    };
    let event = Event::from_draft(
        "evt_00000000000000000000000000000001",
        1_786_422_000_000,
        draft,
    );
    assert_eq!(event.id, "evt_00000000000000000000000000000001");
    assert_eq!(event.timestamp_ms, 1_786_422_000_000);
    assert_eq!(event.idempotency_key.as_deref(), Some("example:1"));
    assert_eq!(event.cursor, None);
}

#[test]
fn every_semantic_field_changes_the_entry_address() {
    let event = event("evt_base");
    let baseline = address_for(&event, None);

    let mut changed = event.clone();
    changed.id = "evt_changed".to_owned();
    assert_ne!(address_for(&changed, None), baseline);

    let mut changed = event.clone();
    changed.event_type = "example.changed".to_owned();
    assert_ne!(address_for(&changed, None), baseline);

    let mut changed = event.clone();
    changed.source = "other".to_owned();
    assert_ne!(address_for(&changed, None), baseline);

    let mut changed = event.clone();
    changed.payload = fields(json!({"value": 2}));
    assert_ne!(address_for(&changed, None), baseline);

    let mut changed = event.clone();
    changed.idempotency_key = None;
    assert_ne!(address_for(&changed, None), baseline);

    let mut changed = event.clone();
    changed.caused_by = Some("evt_parent".to_owned());
    assert_ne!(address_for(&changed, None), baseline);

    let mut changed = event.clone();
    changed.session_id = None;
    assert_ne!(address_for(&changed, None), baseline);

    let mut changed = event.clone();
    changed.run_id = None;
    assert_ne!(address_for(&changed, None), baseline);

    let mut changed = event.clone();
    changed.turn_id = None;
    assert_ne!(address_for(&changed, None), baseline);

    let mut changed = event.clone();
    changed.timestamp_ms += 1;
    assert_ne!(address_for(&changed, None), baseline);

    let previous = Hash::from_bytes([1; 32]);
    assert_ne!(address_for(&event, Some(&previous)), baseline);

    let mut changed = event;
    changed.cursor = Some(99);
    assert_eq!(address_for(&changed, None), baseline);
}

#[test]
fn canonical_payload_rejects_numbers_outside_the_identity_domain() {
    let payload = serde_json::from_str("{\"value\":18446744073709551616}").unwrap();
    assert!(canonical_payload(&payload).is_err());
}

#[test]
fn new_events_require_identity_fields_and_a_canonical_payload() {
    let mut journal = MemoryJournal::new();

    let mut invalid = event("");
    assert_eq!(journal.append(invalid.clone()), Err(AppendError::EmptyId));

    invalid.id = "evt_1".to_owned();
    invalid.event_type.clear();
    assert_eq!(
        journal.append(invalid.clone()),
        Err(AppendError::EmptyEventType)
    );

    invalid.event_type = "example.created".to_owned();
    invalid.source.clear();
    assert_eq!(
        journal.append(invalid.clone()),
        Err(AppendError::EmptySource)
    );

    invalid.source = "unit".to_owned();
    invalid.payload = serde_json::from_str("{\"value\":18446744073709551616}").unwrap();
    let error = journal.append(invalid).unwrap_err();
    assert_eq!(error.reason(), "payload_encoding");
    assert!(journal.entries().is_empty());
    assert_eq!(journal.head(), None);
}

#[test]
fn duplicate_candidates_are_resolved_before_content_validation() {
    let mut journal = MemoryJournal::new();
    let inserted = journal.append(event("evt_1")).unwrap();
    let head = journal.head();

    let mut duplicate = event("evt_1");
    duplicate.payload = serde_json::from_str("{\"value\":18446744073709551616}").unwrap();
    let outcome = journal.append(duplicate).unwrap();

    assert!(!outcome.inserted);
    assert_eq!(outcome.event, inserted.event);
    assert_eq!(journal.head(), head);
    assert_eq!(journal.entries().len(), 1);
}

#[test]
fn duplicate_candidates_still_require_identity_fields() {
    let mut journal = MemoryJournal::new();
    journal.append(event("evt_1")).unwrap();
    let head = journal.head();

    let mut duplicate = event("evt_retry");
    duplicate.source.clear();
    assert_eq!(journal.append(duplicate), Err(AppendError::EmptySource));
    assert_eq!(journal.head(), head);
    assert_eq!(journal.entries().len(), 1);
}

#[test]
fn verification_reports_the_first_payload_byte_divergence() {
    let mut journal = MemoryJournal::new();
    journal.append(event("evt_1")).unwrap();
    let mut entries = journal.entries().to_vec();
    entries[0].payload_bytes = b"{ \"value\":1}".to_vec();

    assert_eq!(
        verify(&entries, HeadExpectation::Unanchored),
        Err(VerificationError {
            entries_checked: 0,
            event_id: Some("evt_1".to_owned()),
            cursor: Some(1),
            kind: VerificationErrorKind::PayloadEncoding,
        })
    );
}

#[test]
fn verification_checks_cursor_and_uniqueness_before_content() {
    let mut journal = MemoryJournal::new();
    journal.append(event("evt_1")).unwrap();
    let mut second = event("evt_2");
    second.idempotency_key = Some("example:2".to_owned());
    journal.append(second).unwrap();
    let entries = journal.entries().to_vec();

    let mut changed = entries.clone();
    changed[1].event.cursor = Some(1);
    let error = verify(&changed, HeadExpectation::Unanchored).unwrap_err();
    assert_eq!(error.reason(), "cursor_order");
    assert_eq!(
        error.kind,
        VerificationErrorKind::CursorOrder {
            previous: Some(1),
            actual: Some(1),
        }
    );

    let mut changed = entries.clone();
    changed[1].event.id = "evt_1".to_owned();
    let error = verify(&changed, HeadExpectation::Unanchored).unwrap_err();
    assert_eq!(error.reason(), "duplicate_id");
    assert_eq!(error.kind, VerificationErrorKind::DuplicateId);

    let mut changed = entries;
    changed[1].event.idempotency_key = Some("example:1".to_owned());
    let error = verify(&changed, HeadExpectation::Unanchored).unwrap_err();
    assert_eq!(error.reason(), "duplicate_idempotency_key");
    assert_eq!(
        error.kind,
        VerificationErrorKind::DuplicateIdempotencyKey {
            key: "example:1".to_owned(),
        }
    );
}

#[test]
fn verification_reports_payload_predecessor_and_entry_mismatches() {
    let mut journal = MemoryJournal::new();
    journal.append(event("evt_1")).unwrap();
    let mut second = event("evt_2");
    second.idempotency_key = Some("example:2".to_owned());
    journal.append(second).unwrap();

    let entries = journal.entries().to_vec();
    let mut changed = entries.clone();
    changed[0].payload_address = Hash::from_bytes([1; 32]);
    let error = verify(&changed, HeadExpectation::Unanchored).unwrap_err();
    assert_eq!(error.entries_checked, 0);
    assert_eq!(error.reason(), "payload_address");
    assert_eq!(
        error.kind,
        VerificationErrorKind::PayloadAddressMismatch {
            expected: entries[0].payload_address,
            actual: Hash::from_bytes([1; 32]),
        }
    );

    let mut changed = entries.clone();
    changed[1].previous_address = None;
    let error = verify(&changed, HeadExpectation::Unanchored).unwrap_err();
    assert_eq!(error.entries_checked, 1);
    assert_eq!(error.reason(), "previous_address");
    assert_eq!(
        error.kind,
        VerificationErrorKind::PreviousAddressMismatch {
            expected: Some(entries[0].entry_address),
            actual: None,
        }
    );

    let mut changed = entries;
    changed[1].entry_address = Hash::from_bytes([2; 32]);
    let expected = entry_address(
        &changed[1].event,
        &changed[1].payload_address,
        changed[1].previous_address.as_ref(),
    )
    .unwrap();
    let error = verify(&changed, HeadExpectation::Unanchored).unwrap_err();
    assert_eq!(error.entries_checked, 1);
    assert_eq!(error.reason(), "entry_address");
    assert_eq!(
        error.kind,
        VerificationErrorKind::EntryAddressMismatch {
            expected: Some(expected),
            actual: Hash::from_bytes([2; 32]),
        }
    );
}

#[test]
fn expected_head_detects_tail_truncation() {
    let mut journal = MemoryJournal::new();
    journal.append(event("evt_1")).unwrap();
    let expected = Hash::from_bytes([3; 32]);
    let actual = journal.head();

    let error = verify(journal.entries(), HeadExpectation::Exact(Some(&expected))).unwrap_err();
    assert_eq!(error.reason(), "expected_head");
    assert_eq!(
        error,
        VerificationError {
            entries_checked: 1,
            event_id: Some("evt_1".to_owned()),
            cursor: Some(1),
            kind: VerificationErrorKind::ExpectedHeadMismatch {
                expected: Some(expected),
                actual,
            },
        }
    );
}

#[test]
fn an_exact_empty_head_detects_an_unexpected_entry() {
    let empty: Vec<JournalEntry> = Vec::new();
    let report = verify(&empty, HeadExpectation::Exact(None)).unwrap();
    assert_eq!(report.head, None);

    let mut journal = MemoryJournal::new();
    journal.append(event("evt_1")).unwrap();
    let error = verify(journal.entries(), HeadExpectation::Exact(None)).unwrap_err();
    assert_eq!(error.reason(), "expected_head");
}

#[test]
fn journal_entry_constructor_assigns_cursor_and_chain_metadata() {
    let entry = JournalEntry::new(event("evt_1"), 7, None).unwrap();
    assert_eq!(entry.event.cursor, Some(7));
    assert_eq!(entry.previous_address, None);
    assert_eq!(entry.payload_address, payload_address(&entry.payload_bytes));
}
