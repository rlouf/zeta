"""Conformance tests that consume the shared IPC vectors directly."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest
import zeta.ipc.client as ipc_client
from ipc_test_support import VECTORS_DIR
from zeta.ipc.framing import FrameReader, FrameViolation, decode_frame, encode_frame
from zeta.ipc.messages import (
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PROTOCOL_VERSION,
    SERVER_ERROR,
    MessageError,
    error_response,
    message_kind,
    request,
    success_response,
    validate_initialize_result,
    validate_message,
)

VALID_DIR = VECTORS_DIR / "ipc" / "messages" / "valid"
INVALID_DIR = VECTORS_DIR / "ipc" / "messages" / "invalid"
SESSIONS_DIR = VECTORS_DIR / "ipc" / "sessions"
CAPABILITY_PROVIDER_SESSION = SESSIONS_DIR / "capability-provider.jsonl"


def json_paths(directory: Path) -> list[Path]:
    paths = sorted(directory.glob("*.json"))
    assert paths, f"JSON vectors are missing from {directory}"
    return paths


@pytest.mark.parametrize("path", json_paths(VALID_DIR), ids=lambda path: path.stem)
def test_valid_message_vectors_parse_and_encode_compactly(path: Path) -> None:
    raw = path.read_bytes()
    message = validate_message(json.loads(raw))
    assert encode_frame(message) == raw.rstrip(b"\n") + b"\n"


@pytest.mark.parametrize("path", json_paths(INVALID_DIR), ids=lambda path: path.stem)
def test_invalid_message_vectors_report_the_documented_code(path: Path) -> None:
    expected_code = int(
        path.with_suffix(".reason.txt").read_text(encoding="utf-8").splitlines()[0]
    )
    with pytest.raises(MessageError) as failure:
        validate_message(json.loads(path.read_bytes()))
    assert failure.value.code == expected_code


@pytest.mark.parametrize(
    "path",
    sorted(SESSIONS_DIR.glob("*.jsonl")),
    ids=lambda path: path.stem,
)
def test_session_vectors_have_direction_and_valid_messages(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        assert value.pop("_dir") in {"peer_to_runtime", "runtime_to_peer"}
        validate_message(value)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ({"jsonrpc": "2.0", "id": 1, "method": "ping"}, "request"),
        ({"jsonrpc": "2.0", "method": "peer.ready"}, "notification"),
        ({"jsonrpc": "2.0", "id": 1, "result": None}, "success"),
        (
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            },
            "error",
        ),
    ],
)
def test_message_classification_uses_jsonrpc_discriminants(
    message: dict, expected: str
) -> None:
    assert message_kind(validate_message(message)) == expected


@pytest.mark.parametrize(
    "message_id",
    ["request-1", 0, -1, 2**64 - 1, -(2**63)],
)
def test_request_ids_accept_strings_and_integral_i64_u64(message_id: str | int) -> None:
    validate_message(request(message_id, "ping"))


@pytest.mark.parametrize(
    "message_id",
    [None, True, 1.5, [], {}, 2**64, -(2**63) - 1],
)
def test_request_ids_reject_values_outside_the_profile(message_id: object) -> None:
    with pytest.raises(MessageError) as failure:
        validate_message(
            {"jsonrpc": "2.0", "id": message_id, "method": "ping", "params": {}}
        )
    assert failure.value.code == INVALID_REQUEST


def test_initialize_result_rejects_malformed_roles_as_a_message_error() -> None:
    result = {
        "protocol_version": 0,
        "runtime": {"name": "zeta", "version": "0.1.0"},
        "roles": [["source"]],
        "config": {},
        "heartbeat_seconds": 10,
        "max_in_flight": 64,
    }
    with pytest.raises(MessageError):
        validate_initialize_result(result, ["source"])


def test_decode_frame_reports_parse_and_request_failures() -> None:
    malformed = decode_frame(b"not json\n")
    invalid = decode_frame(b'{"jsonrpc":"2.0","id":null,"method":"ping"}\n')
    assert isinstance(malformed, FrameViolation)
    assert malformed.code == -32700
    assert isinstance(invalid, FrameViolation)
    assert invalid.code == -32600


def test_invalid_request_frame_recovers_a_valid_request_id() -> None:
    invalid = decode_frame(
        b'{"jsonrpc":"2.0","id":"bad-params","method":"session.list",'
        b'"params":{"unexpected":true}}\n'
    )

    assert isinstance(invalid, FrameViolation)
    assert invalid.code == -32602
    assert invalid.request_id == "bad-params"


async def test_frame_reader_accepts_a_complete_final_object_at_eof() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}')
    reader.feed_eof()
    frame = await FrameReader(reader).read_frame()
    assert isinstance(frame, dict)
    frame = cast(dict[str, Any], frame)
    assert frame["method"] == "ping"


async def test_frame_reader_recovers_after_an_oversized_line() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"x" * 65 + b"\n")
    reader.feed_data(encode_frame(request(1, "ping")))
    reader.feed_eof()
    frames = FrameReader(reader, max_frame_bytes=64)
    violation = await frames.read_frame()
    recovered = await frames.read_frame()
    assert isinstance(violation, FrameViolation)
    assert violation.rule == "frame_too_long"
    assert isinstance(recovered, dict)
    recovered = cast(dict[str, Any], recovered)
    assert recovered["method"] == "ping"


def directed_message(direction: str, message: dict[str, Any]) -> dict[str, Any]:
    return {"_dir": direction, **message}


def python_capability_provider_session_vector() -> list[dict[str, Any]]:
    initialization = request(
        "peer-initialize",
        "initialize",
        {
            "protocol_versions": [PROTOCOL_VERSION],
            "peer": {"name": "reference-provider", "version": "0.0.1"},
            "roles": ["provider"],
            "heartbeat_seconds": ipc_client.DEFAULT_HEARTBEAT_SECONDS,
            "max_in_flight": ipc_client.DEFAULT_MAX_IN_FLIGHT,
            "methods": [{"name": "zeta.read"}],
        },
    )
    initialized = success_response(
        "peer-initialize",
        {
            "protocol_version": PROTOCOL_VERSION,
            "runtime": {"name": "zeta", "version": "0.1.0"},
            "roles": ["provider"],
            "config": {},
            "heartbeat_seconds": ipc_client.DEFAULT_HEARTBEAT_SECONDS,
            "max_in_flight": ipc_client.DEFAULT_MAX_IN_FLIGHT,
        },
    )
    return [
        directed_message("peer_to_runtime", initialization),
        directed_message("runtime_to_peer", initialized),
        directed_message(
            "runtime_to_peer",
            request(
                "runtime-1",
                "zeta.read",
                {
                    "input": {"path": "notes.md"},
                    "base_dir": "/workspace/zeta",
                    "effect_key": None,
                },
            ),
        ),
        directed_message(
            "peer_to_runtime",
            success_response(
                "runtime-1",
                {
                    "ok": True,
                    "content": [{"type": "text", "text": "notes"}],
                    "metadata": {"path": "notes.md"},
                },
            ),
        ),
        directed_message(
            "runtime_to_peer",
            request(
                "runtime-2",
                "zeta.read",
                {
                    "input": {"path": "missing.md"},
                    "base_dir": "/workspace/zeta",
                    "effect_key": None,
                },
            ),
        ),
        directed_message(
            "peer_to_runtime",
            error_response(
                "runtime-2",
                SERVER_ERROR,
                "provider rejected the request",
                {"code": "provider_rejected", "retryable": True},
            ),
        ),
        directed_message(
            "runtime_to_peer",
            request(
                "runtime-3",
                "undeclared.method",
                {"input": {}, "base_dir": None, "effect_key": None},
            ),
        ),
        directed_message(
            "peer_to_runtime",
            error_response(
                "runtime-3",
                METHOD_NOT_FOUND,
                "Method not found",
                {"code": "method_not_found"},
            ),
        ),
        directed_message(
            "runtime_to_peer",
            request("runtime-4", "shutdown", {"reason": "runtime stopping"}),
        ),
        directed_message(
            "peer_to_runtime",
            success_response("runtime-4", {}),
        ),
    ]


def test_capability_provider_session_matches_python_ground_truth() -> None:
    expected = [
        json.loads(line)
        for line in CAPABILITY_PROVIDER_SESSION.read_text(encoding="utf-8").splitlines()
    ]
    assert python_capability_provider_session_vector() == expected
