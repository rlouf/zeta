//! In-memory reference behavior independent of a storage backend.

use serde_json::{json, Map, Value};
use zeta_journal::{Event, EventFilter, MemoryJournal};

fn fields(value: Value) -> Map<String, Value> {
    value.as_object().unwrap().clone()
}

fn event(id: &str, key: Option<&str>, caused_by: Option<&str>) -> Event {
    Event {
        id: id.to_owned(),
        event_type: "prefix.literal_%done".to_owned(),
        source: "memory-test".to_owned(),
        payload: fields(json!({"id": id})),
        idempotency_key: key.map(str::to_owned),
        caused_by: caused_by.map(str::to_owned),
        session_id: Some("session/1".to_owned()),
        run_id: Some("run_1".to_owned()),
        turn_id: Some("turn_1".to_owned()),
        timestamp_ms: 1,
        cursor: None,
    }
}

#[test]
fn duplicate_resolution_prefers_id_and_keeps_head_and_cursor() {
    let mut journal = MemoryJournal::new();
    let first = journal.append(event("evt_1", Some("key:1"), None)).unwrap();
    let second = journal.append(event("evt_2", Some("key:2"), None)).unwrap();
    let head = journal.head();

    let duplicate = journal.append(event("evt_1", Some("key:2"), None)).unwrap();
    assert!(!duplicate.inserted);
    assert_eq!(duplicate.event.id, first.event.id);
    assert_eq!(duplicate.event.cursor, Some(1));
    assert_eq!(journal.head(), head);
    assert_eq!(journal.entries().len(), 2);

    let duplicate = journal.append(event("evt_3", Some("key:2"), None)).unwrap();
    assert!(!duplicate.inserted);
    assert_eq!(duplicate.event.id, second.event.id);
    assert_eq!(duplicate.event.cursor, Some(2));
    assert_eq!(journal.head(), head);
}

#[test]
fn query_helpers_preserve_cursor_order_and_literal_prefixes() {
    let mut journal = MemoryJournal::new();
    journal.append(event("evt_1", None, None)).unwrap();
    journal.append(event("evt_2", None, Some("evt_1"))).unwrap();
    let mut third = event("evt_3", None, Some("evt_2"));
    third.event_type = "prefixXliteralZdone".to_owned();
    journal.append(third).unwrap();

    let filter = EventFilter {
        event_type_prefix: Some("prefix.literal_%".to_owned()),
        ..EventFilter::default()
    };
    let events = journal.list_events(&filter);
    assert_eq!(events.len(), 2);
    assert_eq!(events[0].id, "evt_1");
    assert_eq!(events[1].id, "evt_2");

    let filter = EventFilter {
        after_cursor: Some(1),
        limit: Some(1),
        newest_first: true,
        ..EventFilter::default()
    };
    let events = journal.list_events(&filter);
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].id, "evt_3");

    let children = journal.children("evt_1", None);
    assert_eq!(children.len(), 1);
    assert_eq!(children[0].id, "evt_2");

    let chain = journal.causal_chain("evt_3");
    assert_eq!(chain.len(), 3);
    assert_eq!(chain[0].id, "evt_1");
    assert_eq!(chain[1].id, "evt_2");
    assert_eq!(chain[2].id, "evt_3");
}

#[test]
fn causal_chain_stops_at_missing_and_repeated_ids() {
    let mut journal = MemoryJournal::new();
    journal
        .append(event("evt_orphan", None, Some("evt_missing")))
        .unwrap();
    journal.append(event("evt_a", None, Some("evt_b"))).unwrap();
    journal.append(event("evt_b", None, Some("evt_a"))).unwrap();

    let chain = journal.causal_chain("evt_orphan");
    assert_eq!(chain.len(), 1);
    assert_eq!(chain[0].id, "evt_orphan");

    let chain = journal.causal_chain("evt_b");
    assert_eq!(chain.len(), 2);
    assert_eq!(chain[0].id, "evt_a");
    assert_eq!(chain[1].id, "evt_b");

    assert!(journal.causal_chain("evt_absent").is_empty());
}
