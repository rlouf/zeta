//! Conformance against the canonical journal vectors.

use std::path::PathBuf;

use serde_json::Value;
use zeta_journal::{
    canonical_chain_bytes, canonical_payload, entry_address, payload_address, verify, Event,
    EventFilter, HeadExpectation, MemoryJournal,
};
use zeta_substrate::Hash;

fn vectors_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../spec/vectors/journal")
}

fn read_json(name: &str) -> Value {
    let path = vectors_root().join(name);
    let text = std::fs::read_to_string(path).unwrap();
    serde_json::from_str(&text).unwrap()
}

#[test]
fn python_chain_vectors_are_byte_exact() {
    let document = read_json("chain.json");
    let vectors = document["vectors"].as_array().unwrap();
    assert!(!vectors.is_empty());

    for vector in vectors {
        let event: Event = serde_json::from_value(vector["event"].clone()).unwrap();
        let payload = canonical_payload(&event.payload).unwrap();
        assert_eq!(payload, vector["payload_utf8"].as_str().unwrap().as_bytes());

        let payload_address = payload_address(&payload);
        assert_eq!(payload_address.to_string(), vector["payload_address"]);

        let previous_address = vector["previous_address"]
            .as_str()
            .map(|address| address.parse::<Hash>().unwrap());
        let canonical =
            canonical_chain_bytes(&event, &payload_address, previous_address.as_ref()).unwrap();
        assert_eq!(
            canonical,
            vector["canonical_utf8"].as_str().unwrap().as_bytes()
        );
        assert_eq!(
            entry_address(&event, &payload_address, previous_address.as_ref())
                .unwrap()
                .to_string(),
            vector["entry_address"]
        );
    }
}

#[test]
fn python_operation_vectors_replay_exactly() {
    let document = read_json("operations.json");
    let mut journal = MemoryJournal::new();

    for append in document["appends"].as_array().unwrap() {
        let event: Event = serde_json::from_value(append["event"].clone()).unwrap();
        let expected = &append["expected"];
        if let Some(reason) = expected["error"].as_str() {
            let head = journal.head();
            let error = journal.append(event).unwrap_err();
            assert_eq!(error.reason(), reason, "{}", append["name"]);
            assert_eq!(journal.head(), head, "{}", append["name"]);
            continue;
        }
        let outcome = journal.append(event).unwrap();
        assert_eq!(outcome.inserted, expected["inserted"].as_bool().unwrap());
        assert_eq!(outcome.event.id, expected["returned_id"]);
        assert_eq!(
            outcome.event.cursor,
            Some(expected["cursor"].as_u64().unwrap())
        );
        assert_eq!(
            journal.head().unwrap().to_string(),
            expected["head"].as_str().unwrap()
        );
    }

    for query in document["queries"].as_array().unwrap() {
        let filter: EventFilter = serde_json::from_value(query["filter"].clone()).unwrap();
        let events = journal.list_events(&filter);
        let mut ids = Vec::new();
        for event in events {
            ids.push(event.id.as_str());
        }
        let mut expected_ids = Vec::new();
        for id in query["expected_ids"].as_array().unwrap() {
            expected_ids.push(id.as_str().unwrap());
        }
        assert_eq!(ids, expected_ids, "{}", query["name"]);
    }

    for chain in document["causal_chains"].as_array().unwrap() {
        let events = journal.causal_chain(chain["event_id"].as_str().unwrap());
        let mut ids = Vec::new();
        for event in events {
            ids.push(event.id.as_str());
        }
        let mut expected_ids = Vec::new();
        for id in chain["expected_ids"].as_array().unwrap() {
            expected_ids.push(id.as_str().unwrap());
        }
        assert_eq!(ids, expected_ids, "{}", chain["name"]);
    }

    let expected_head = document["final_head"].as_str().unwrap().parse().unwrap();
    assert_eq!(journal.head(), Some(expected_head));
    let report = verify(
        journal.entries(),
        HeadExpectation::Exact(Some(&expected_head)),
    )
    .unwrap();
    assert_eq!(report.entries_checked, 7);
    assert_eq!(report.head, Some(expected_head));
}
