"""Conformance tests that consume spec/vectors directly."""

import json
from typing import Any, cast

import pytest
from wire_test_support import (
    VECTORS_DIR,
    complete_handshake,
    finished,
    frame_reader,
    read_envelope,
    sdk_child_source,
    send,
    spawn,
    write_child,
)
from zeta.ids import event_idempotency_id
from zeta.wire.envelopes import (
    EnvelopeError,
    canonical_json,
    envelope,
    mint_event_id,
    validate_envelope,
)

VALID_DIR = VECTORS_DIR / "envelopes" / "valid"
INVALID_DIR = VECTORS_DIR / "envelopes" / "invalid"
SESSION_PATH = VECTORS_DIR / "handshake" / "session-01.jsonl"


def test_wire_event_ids_delegate_to_runtime_identity_ownership() -> None:
    payload = {"path": "inbox/todo.txt", "size": 12}
    assert mint_event_id("file.created", payload) == event_idempotency_id(
        "file.created", payload
    )


@pytest.mark.parametrize(
    "value",
    [pytest.param(2**64, id="above-u64"), pytest.param(-(2**63) - 1, id="below-i64")],
)
def test_wire_event_identity_rejects_integers_outside_the_canonical_domain(
    value: int,
) -> None:
    with pytest.raises(ValueError, match="integers must fit i64 or u64"):
        mint_event_id("file.created", {"value": value})

    event = envelope(
        "event",
        "m-domain-int",
        type="file.created",
        schema="zeta.file.created.v1",
        caused_by=None,
        session_id=None,
        payload={"value": value},
    )
    with pytest.raises(EnvelopeError, match="bad_canonical_value"):
        validate_envelope(event)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
    ],
)
def test_wire_event_identity_rejects_nonfinite_floats(value: float) -> None:
    with pytest.raises(ValueError, match="floats must be finite"):
        event_idempotency_id("file.created", {"value": value})

    event = envelope(
        "event",
        "m-domain-float",
        type="file.created",
        schema="zeta.file.created.v1",
        caused_by=None,
        session_id=None,
        payload={"value": value},
    )
    with pytest.raises(EnvelopeError, match="bad_canonical_value"):
        validate_envelope(event)


def test_wire_event_identity_rejects_non_string_object_keys() -> None:
    payload = cast(dict[str, Any], {1: "value"})
    with pytest.raises(TypeError, match="object keys must be strings"):
        mint_event_id("file.created", payload)

    event = envelope(
        "event",
        "m-domain-key",
        type="file.created",
        schema="zeta.file.created.v1",
        caused_by=None,
        session_id=None,
        payload=payload,
    )
    with pytest.raises(EnvelopeError, match="bad_canonical_value"):
        validate_envelope(event)


def valid_vector_paths() -> list:
    paths = sorted(VALID_DIR.glob("*.json"))
    assert paths, "valid envelope vectors are missing"
    return paths


def invalid_vector_paths() -> list:
    paths = sorted(INVALID_DIR.glob("*.json"))
    assert paths, "invalid envelope vectors are missing"
    return paths


@pytest.mark.parametrize("path", valid_vector_paths(), ids=lambda p: p.stem)
def test_valid_vector_parses_and_reserializes_canonically(path) -> None:
    raw = path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    validate_envelope(parsed)
    assert canonical_json(parsed) + "\n" == raw


@pytest.mark.parametrize("path", invalid_vector_paths(), ids=lambda p: p.stem)
def test_invalid_vector_is_rejected_for_the_documented_rule(path) -> None:
    reason_path = path.with_name(path.stem + ".reason.txt")
    documented_rule = reason_path.read_text(encoding="utf-8").splitlines()[0]
    parsed = json.loads(path.read_text(encoding="utf-8"))
    with pytest.raises(EnvelopeError) as failure:
        validate_envelope(parsed)
    assert failure.value.rule == documented_rule


RULE_INVENTORY = frozenset(
    {
        "not_an_object",
        "missing_field:v",
        "bad_version",
        "missing_field:kind",
        "bad_kind",
        "missing_field:id",
        "bad_id",
        "missing_field:ts",
        "bad_timestamp",
        "reserved_kind",
        "unknown_kind",
        "missing_field:name",
        "bad_name",
        "missing_field:plugin_version",
        "bad_plugin_version",
        "missing_field:role",
        "bad_role",
        "missing_field:protocol_versions",
        "bad_protocol_versions",
        "missing_field:event_types",
        "bad_event_types",
        "bad_operations",
        "bad_capabilities",
        "bad_heartbeat_secs",
        "bad_ack_window",
        "missing_field:protocol_version",
        "bad_protocol_version",
        "missing_field:runtime",
        "bad_runtime",
        "bad_config",
        "missing_field:type",
        "bad_type",
        "missing_field:schema",
        "bad_schema",
        "missing_field:caused_by",
        "bad_caused_by",
        "missing_field:session_id",
        "bad_session_id",
        "payload_choice",
        "bad_payload",
        "payload_too_large",
        "bad_payload_hash",
        "missing_field:event_id",
        "bad_event_id",
        "missing_field:code",
        "bad_code",
        "missing_field:message",
        "bad_message",
        "missing_field:retryable",
        "bad_retryable",
        "bad_reason",
        "missing_field:payload",
        "missing_field:effect_key",
        "bad_effect_key",
        "missing_field:call_id",
        "bad_call_id",
        "missing_field:ok",
        "bad_ok",
        "result_choice",
        "bad_error",
    }
)


def test_every_validator_rule_has_an_invalid_vector() -> None:
    """The exhaustiveness tripwire.

    The inventory mirrors every rule token the envelope validator can
    emit. A rule added to the validator without a golden vector — or a
    vector whose token the validator cannot produce — fails here, so
    the two language implementations cannot drift on a rule the
    vectors never exercise.
    """
    covered = set()
    for path in invalid_vector_paths():
        reason_path = path.with_name(path.stem + ".reason.txt")
        covered.add(reason_path.read_text(encoding="utf-8").splitlines()[0])
    assert covered == RULE_INVENTORY


def session_lines() -> list[tuple[str, dict]]:
    lines = []
    for line in SESSION_PATH.read_text(encoding="utf-8").splitlines():
        parsed = json.loads(line)
        direction = parsed.pop("_dir")
        lines.append((direction, parsed))
    return lines


def test_session_vector_envelopes_validate_after_direction_strip() -> None:
    lines = session_lines()
    assert [direction for direction, _ in lines] == [
        "c2p",
        "p2c",
        "c2p",
        "p2c",
        "c2p",
        "p2c",
        "c2p",
        "p2c",
    ]
    for _direction, envelope_value in lines:
        validate_envelope(envelope_value)


@pytest.mark.parametrize(
    "path",
    sorted((VECTORS_DIR / "handshake").glob("*.jsonl")),
    ids=lambda p: p.stem,
)
def test_every_session_vector_validates_after_direction_strip(path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = json.loads(line)
        assert parsed.pop("_dir") in {"c2p", "p2c"}
        validate_envelope(parsed)


def test_session_vector_event_ids_follow_the_minting_rule() -> None:
    for _direction, envelope_value in session_lines():
        if envelope_value["kind"] == "event":
            assert envelope_value["id"] == mint_event_id(
                envelope_value["type"], envelope_value["payload"]
            )


async def test_session_replays_through_the_plugin_side(tmp_path) -> None:
    """A run_source child reproduces the session's child half."""
    session = session_lines()
    child_events = [e for d, e in session if d == "c2p" and e["kind"] == "event"]
    payloads = [event["payload"] for event in child_events]
    script = write_child(
        tmp_path,
        sdk_child_source(
            events_body=f"""
            import asyncio
            for payload in {payloads!r}:
                yield SourceEvent("file.created", payload)
            await asyncio.sleep(60)
            """,
        ),
    )
    process = await spawn(script)
    reader = frame_reader(process)
    hello = await complete_handshake(process, reader)
    session_hello = session[0][1]
    for field in ("name", "plugin_version", "role", "protocol_versions", "event_types"):
        assert hello[field] == session_hello[field]
    for expected in child_events:
        received = await read_envelope(reader)
        assert received["kind"] == "event"
        for field in ("id", "type", "schema", "caused_by", "session_id", "payload"):
            assert received[field] == expected[field]
        ack = next(
            e for d, e in session if d == "p2c" and e.get("event_id") == expected["id"]
        )
        await send(process, ack)
    shutdown = next(e for _d, e in session if e["kind"] == "shutdown")
    await send(process, shutdown)
    assert await finished(process) == 0


async def test_session_replays_through_the_runtime_side(tmp_path) -> None:
    """SubprocessSource accepts the session's child half and acks it."""
    from zeta.wire.host import SourceCommand, SubprocessSource

    session = session_lines()
    child_events = [e for d, e in session if d == "c2p" and e["kind"] == "event"]
    script = write_child(
        tmp_path,
        f"""
        import json, sys

        lines = {[canonical_json(e) for d, e in session if d == "c2p"]!r}
        events = [json.loads(line) for line in lines if '"kind":"event"' in line]
        sys.stdout.write(lines[0] + "\\n")
        sys.stdout.flush()
        ack = json.loads(sys.stdin.readline())
        assert ack["kind"] == "hello_ack", ack
        for line in lines[1:]:
            sys.stdout.write(line + "\\n")
            sys.stdout.flush()
            if '"kind":"event"' in line:
                ack = json.loads(sys.stdin.readline())
                assert ack["kind"] == "ack", ack
                assert ack["event_id"] == json.loads(line)["id"], ack
        while True:
            message = json.loads(sys.stdin.readline())
            if message["kind"] == "shutdown":
                raise SystemExit(0)
        """,
    )
    import sys

    received = []
    async with SubprocessSource(
        SourceCommand((sys.executable, str(script))),
        runtime_id="zeta-test/0",
        max_restarts=0,
    ) as source:
        async for event in source.events():
            received.append(event)
            await source.ack(event.id)
            if len(received) == len(child_events):
                break
    assert [event.id for event in received] == [e["id"] for e in child_events]
    assert [event.payload for event in received] == [e["payload"] for e in child_events]
