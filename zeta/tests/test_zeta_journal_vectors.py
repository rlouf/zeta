import json
from dataclasses import replace
from pathlib import Path

import pytest
from zeta.events import Event, json_native_payload
from zeta.journal import types as journal_types
from zeta.journal.memory import MemoryEventStore
from zeta.journal.sqlite import SqliteEventStore
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


def _vector_entries() -> list[journal_types.JournalEntry]:
    entries = []
    previous_address = None
    for cursor, vector in enumerate(_load_vector("chain.json")["vectors"], start=1):
        event = replace(_event_from_mapping(vector["event"]), cursor=cursor)
        entry = journal_types.journal_entry(event, previous_address)
        entries.append(entry)
        previous_address = entry.entry_address
    return entries


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


@pytest.mark.parametrize(
    ("payload", "error_type"),
    [
        ({1: "not a string key"}, TypeError),
        ({"value": float("nan")}, ValueError),
        ({"value": float("inf")}, ValueError),
        ({"value": -(2**63) - 1}, ValueError),
        ({"value": 2**64}, ValueError),
        ({"value": object()}, TypeError),
    ],
)
def test_json_native_payload_rejects_values_outside_identity_domain(
    payload: dict,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        json_native_payload(payload)


def test_journal_entries_reproduce_the_frozen_chain_vectors() -> None:
    vectors = _load_vector("chain.json")["vectors"]
    entries = _vector_entries()
    for entry, vector in zip(entries, vectors, strict=True):
        assert entry.payload_bytes == vector["payload_utf8"].encode()
        assert entry.payload_address == vector["payload_address"]
        assert entry.previous_address == vector["previous_address"]
        assert entry.entry_address == vector["entry_address"]


def test_every_semantic_event_field_changes_the_entry_address() -> None:
    base = Event(
        id="evt_base",
        event_type="base.event",
        source="fixture",
        payload={"value": 1},
        idempotency_key=None,
        caused_by=None,
        session_id=None,
        run_id=None,
        turn_id=None,
        timestamp_ms=1,
        cursor=1,
    )
    variants = [
        base,
        replace(base, id="evt_changed"),
        replace(base, event_type="changed.event"),
        replace(base, source="changed"),
        replace(base, payload={"value": 2}),
        replace(base, idempotency_key="key"),
        replace(base, caused_by="evt_parent"),
        replace(base, session_id="session"),
        replace(base, run_id="run"),
        replace(base, turn_id="turn"),
        replace(base, timestamp_ms=2),
    ]
    entry_addresses = {
        journal_types.journal_entry(event, None).entry_address for event in variants
    }
    assert len(entry_addresses) == len(variants)


def test_journal_verification_accepts_a_valid_chain_and_trusted_head() -> None:
    entries = _vector_entries()
    report = journal_types.verify_entries(
        entries,
        expected_head=entries[-1].entry_address,
    )
    assert report.entries_checked == len(entries)
    assert report.head_address == entries[-1].entry_address


def test_journal_verification_reports_every_divergence_reason() -> None:
    first, second, _ = _vector_entries()
    duplicate_id = journal_types.journal_entry(
        replace(second.event, id=first.event.id),
        first.entry_address,
    )
    first_with_key = journal_types.journal_entry(
        replace(first.event, idempotency_key=second.event.idempotency_key),
        None,
    )
    second_with_duplicate_key = journal_types.journal_entry(
        second.event,
        first_with_key.entry_address,
    )
    cases = [
        (
            journal_types.VerificationReason.CURSOR_ORDER,
            [first, replace(second, event=replace(second.event, cursor=1))],
        ),
        (journal_types.VerificationReason.DUPLICATE_ID, [first, duplicate_id]),
        (
            journal_types.VerificationReason.DUPLICATE_IDEMPOTENCY_KEY,
            [first_with_key, second_with_duplicate_key],
        ),
        (
            journal_types.VerificationReason.PAYLOAD_ENCODING,
            [replace(first, payload_bytes=b'{"z":null,"a":{"items":[]}}')],
        ),
        (
            journal_types.VerificationReason.PAYLOAD_ADDRESS,
            [replace(first, payload_address=addresses.content_address(b"wrong"))],
        ),
        (
            journal_types.VerificationReason.PREVIOUS_ADDRESS,
            [first, replace(second, previous_address=None)],
        ),
        (
            journal_types.VerificationReason.ENTRY_ADDRESS,
            [replace(first, entry_address=addresses.content_address(b"wrong"))],
        ),
    ]
    for reason, entries in cases:
        with pytest.raises(journal_types.VerificationError) as raised:
            journal_types.verify_entries(entries)
        assert raised.value.reason is reason


def test_trusted_head_detects_tail_truncation() -> None:
    entries = _vector_entries()
    with pytest.raises(journal_types.VerificationError) as raised:
        journal_types.verify_entries(
            entries[:-1],
            expected_head=entries[-1].entry_address,
        )
    assert raised.value.reason is journal_types.VerificationReason.EXPECTED_HEAD
    assert raised.value.entries_checked == len(entries) - 1


@pytest.mark.parametrize("limit", [-1, True])
def test_filter_rejects_invalid_limits(limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        Filter(limit=limit)


@pytest.mark.parametrize(
    "store_factory",
    [
        pytest.param(MemoryEventStore, id="memory"),
        pytest.param(lambda: SqliteEventStore(":memory:"), id="sqlite"),
    ],
)
def test_stores_validate_new_events_but_ignore_duplicate_content(store_factory) -> None:
    store = store_factory()
    event = Event(
        id="evt_valid",
        event_type="valid.event",
        source="fixture",
        payload={"value": 1},
        idempotency_key="valid-key",
        caused_by=None,
        session_id=None,
        run_id=None,
        turn_id=None,
        timestamp_ms=1,
    )
    inserted = store.append(event)
    duplicate = store.append(replace(event, payload={"value": float("nan")}))
    assert inserted.inserted
    assert not duplicate.inserted
    assert duplicate.event == inserted.event
    with pytest.raises(ValueError):
        store.append(
            replace(
                event,
                id="evt_invalid",
                idempotency_key=None,
                payload={"value": float("nan")},
            )
        )
    with pytest.raises(ValueError, match="id"):
        store.append(replace(event, id="", idempotency_key=None))
    store.close()
