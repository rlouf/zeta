import json
from dataclasses import replace
from pathlib import Path

from zeta.events import Event
from zeta.journal.memory import MemoryEventStore
from zeta.journal.store import Filter

from zeta import addresses

VECTORS_DIR = Path(__file__).resolve().parents[2] / "spec" / "vectors" / "journal"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "phase2-journal"


def _load_vector(name: str) -> dict:
    return json.loads((VECTORS_DIR / name).read_text(encoding="utf-8"))


def _event_from_mapping(value: dict) -> Event:
    return Event(
        id=value["id"],
        event_type=value["type"],
        source=value["source"],
        payload=value["payload"],
        idempotency_key=value["idempotency_key"],
        caused_by=value["caused_by"],
        session_id=value["session_id"],
        run_id=value["run_id"],
        turn_id=value["turn_id"],
        timestamp_ms=value["timestamp_ms"],
    )


def _chain_input(
    event: Event,
    payload_address: str,
    previous_address: str | None,
) -> list:
    return [
        0,
        event.id,
        event.event_type,
        event.source,
        payload_address,
        event.idempotency_key,
        event.caused_by,
        event.session_id,
        event.run_id,
        event.turn_id,
        event.timestamp_ms,
        previous_address,
    ]


def _entry_address(event: Event, previous_address: str | None) -> str:
    payload_bytes = addresses.canonical_json_bytes(event.payload)
    payload_address = addresses.content_address(payload_bytes)
    encoded = addresses.canonical_json_bytes(
        _chain_input(event, payload_address, previous_address)
    )
    return addresses.chain_address(encoded)


def test_python_primitives_match_journal_chain_vectors_byte_for_byte() -> None:
    document = _load_vector("chain.json")
    assert document["format"] == "zeta-journal-chain-v0"
    assert document["context"] == addresses.CHAIN_CONTEXT
    assert document["vectors"]
    for vector in document["vectors"]:
        event = _event_from_mapping(vector["event"])
        payload_bytes = addresses.canonical_json_bytes(event.payload)
        assert payload_bytes == vector["payload_utf8"].encode(), vector["name"]
        payload_address = addresses.content_address(payload_bytes)
        assert payload_address == vector["payload_address"], vector["name"]
        chain_input = _chain_input(
            event,
            payload_address,
            vector["previous_address"],
        )
        assert chain_input == vector["chain_input"], vector["name"]
        canonical_bytes = addresses.canonical_json_bytes(chain_input)
        assert canonical_bytes == vector["canonical_utf8"].encode(), vector["name"]
        assert addresses.chain_address(canonical_bytes) == vector["entry_address"]


def test_current_memory_store_matches_journal_operation_vectors() -> None:
    document = _load_vector("operations.json")
    assert document["format"] == "zeta-journal-operations-v0"
    store = MemoryEventStore()
    head = None
    for append in document["appends"]:
        outcome = store.append(_event_from_mapping(append["event"]))
        expected = append["expected"]
        assert outcome.inserted is expected["inserted"], append["name"]
        assert outcome.event.id == expected["returned_id"], append["name"]
        assert outcome.event.cursor == expected["cursor"], append["name"]
        if outcome.inserted:
            head = _entry_address(outcome.event, head)
        assert head == expected["head"], append["name"]
    for query in document["queries"]:
        events = store.list_events(Filter(**query["filter"]))
        assert [event.id for event in events] == query["expected_ids"], query["name"]
    for case in document["causal_chains"]:
        events = store.causal_chain(case["event_id"])
        assert [event.id for event in events] == case["expected_ids"], case["name"]
    assert head == document["final_head"]


def test_frozen_fixture_replays_as_logical_events_without_opening_sqlite() -> None:
    rows = [
        json.loads(line)
        for line in (FIXTURE_DIR / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    store = MemoryEventStore()
    for row in rows:
        incoming = Event(
            id=row["id"],
            event_type=row["type"],
            source=row["source"],
            payload=json.loads(row["payload"]),
            idempotency_key=row["idempotency_key"],
            caused_by=row["caused_by"],
            session_id=row["session_id"],
            run_id=row["run_id"],
            turn_id=row["turn_id"],
            timestamp_ms=row["timestamp"],
            cursor=row["seq"],
        )
        outcome = store.append(incoming)
        assert outcome.inserted
        assert replace(outcome.event, cursor=incoming.cursor) == incoming
    assert [event.id for event in store.list_events(Filter())] == [
        row["id"] for row in rows
    ]
