import json
from dataclasses import asdict, replace
from typing import Any

from zeta.events import DraftEvent, Event
from zeta.history import effect_record, event_from_record, turn_record
from zeta.rpc import (
    generic_rpc_event_from_durable_event,
    rpc_event_from_durable_event,
)
from zeta.runtime_events import (
    model_called_draft,
    model_durable_payload,
    runtime_event_from_event,
    tool_called_draft,
    tool_durable_payload,
)
from zeta.timeline import timeline_from_events

SESSION_ID = "session-1"
TURN_ID = "turn-1"
TIMESTAMP_MICROS = 1_700_000_000_123_456
EVENT_TIME = 1_700_000_000.123456


def event_from_draft(
    draft: DraftEvent,
    *,
    event_id: str,
    seq: int,
) -> Event:
    return replace(
        Event.from_draft(draft),
        id=event_id,
        timestamp_micros=TIMESTAMP_MICROS + seq,
        seq=seq,
    )


def model_event() -> dict[str, Any]:
    return {
        "type": "model",
        "id": "model-1",
        "content": "I will inspect the file.",
        "reasoning": "Need context before editing.",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "read", "arguments": '{"path":"README.md"}'},
            }
        ],
        "prompt_trace": {
            "prompt_object_id": "sha256:prompt",
            "assistant_message_object_id": "sha256:assistant",
        },
        "tool_call_object_ids": ["sha256:tool-call"],
    }


def tool_call_event() -> dict[str, Any]:
    return {
        "type": "tool_call",
        "id": "call-1",
        "tool_call_id": "call-1",
        "status": "pending",
        "name": "read",
        "input": {"path": "README.md"},
        "arguments": '{"path":"README.md"}',
        "caused_by": "model-1",
        "tool_call_object_id": "sha256:tool-call",
    }


def tool_result_event(
    event_id: str,
    *,
    status: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "id": event_id,
        "tool_call_id": "call-1",
        "status": status,
        "name": "read",
        "result": result,
        "capability_id": "sigil.read",
        "model_telemetry": {"input_tokens": 10, "output_tokens": 2},
        "tool_call_object_id": "sha256:tool-call",
        "tool_result_object_id": f"sha256:{event_id}",
    }


def turn_aborted_event() -> dict[str, Any]:
    return {
        "type": "turn_aborted",
        "id": "abort-1",
        "reason": "max_turns",
        "content": "(turn aborted: max turns)",
        "caused_by": "result-ok",
    }


def runtime_drafts() -> list[DraftEvent]:
    model = model_event()
    call = tool_call_event()
    result_ok = tool_result_event(
        "result-ok",
        status="completed",
        result={"ok": True, "content": [{"type": "text", "text": "contents"}]},
    )
    result_failed = tool_result_event(
        "result-failed",
        status="failed",
        result={
            "ok": False,
            "error": {"code": "read_failed", "message": "missing file"},
        },
    )
    result_refused = tool_result_event(
        "result-refused",
        status="refused",
        result={
            "ok": False,
            "refusal": {
                "reason": "capability_not_allowed",
                "message": "read is not allowed",
            },
        },
    )
    aborted = turn_aborted_event()
    return [
        model_called_draft(
            payload=model_durable_payload(model),
            turn_id=TURN_ID,
            session_id=SESSION_ID,
            event_id="model-1",
        ),
        tool_called_draft(
            payload=tool_durable_payload(call),
            turn_id=TURN_ID,
            session_id=SESSION_ID,
            caused_by="model-1",
            event_id="call-1",
        ),
        tool_called_draft(
            payload=tool_durable_payload(result_ok),
            turn_id=TURN_ID,
            session_id=SESSION_ID,
            event_id="result-ok",
        ),
        tool_called_draft(
            payload=tool_durable_payload(result_failed),
            turn_id=TURN_ID,
            session_id=SESSION_ID,
            event_id="result-failed",
        ),
        tool_called_draft(
            payload=tool_durable_payload(result_refused),
            turn_id=TURN_ID,
            session_id=SESSION_ID,
            event_id="result-refused",
        ),
        DraftEvent(
            event_type="zeta.turn.failed",
            source="zeta",
            payload={
                "reason": "max_turns",
                "content": "(turn aborted: max turns)",
                "_timeline_type": "turn_aborted",
            },
            caused_by=aborted["caused_by"],
            session_id=SESSION_ID,
            turn_id=TURN_ID,
        ),
    ]


def runtime_events() -> list[Event]:
    event_ids = [
        "model-1",
        "call-1",
        "result-ok",
        "result-failed",
        "result-refused",
        "abort-1",
    ]
    return [
        event_from_draft(draft, event_id=event_id, seq=index + 1)
        for index, (draft, event_id) in enumerate(
            zip(runtime_drafts(), event_ids, strict=True)
        )
    ]


def test_zeta_runtime_events_project_to_durable_drafts() -> None:
    events = [
        model_event(),
        tool_call_event(),
        tool_result_event(
            "result-ok",
            status="completed",
            result={"ok": True, "content": [{"type": "text", "text": "contents"}]},
        ),
        tool_result_event(
            "result-failed",
            status="failed",
            result={
                "ok": False,
                "error": {"code": "read_failed", "message": "missing file"},
            },
        ),
        tool_result_event(
            "result-refused",
            status="refused",
            result={
                "ok": False,
                "refusal": {
                    "reason": "capability_not_allowed",
                    "message": "read is not allowed",
                },
            },
        ),
        turn_aborted_event(),
    ]

    drafts = []
    for event in events:
        runtime_event = runtime_event_from_event(event)
        assert runtime_event is not None
        drafts.append(runtime_event.to_durable(session_id=SESSION_ID, turn_id=TURN_ID))

    assert [asdict(draft) for draft in drafts] == [
        asdict(draft) for draft in runtime_drafts()
    ]


def test_zeta_runtime_event_projection_contract() -> None:
    drafts = runtime_drafts()

    assert [asdict(draft) for draft in drafts] == [
        {
            "event_type": "zeta.model_call.completed",
            "source": "zeta",
            "payload": {
                "content": "I will inspect the file.",
                "reasoning": "Need context before editing.",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "read",
                            "arguments": '{"path":"README.md"}',
                        },
                    }
                ],
                "_timeline_type": "model",
                "used_objects": [{"kind": "prompt", "id": "sha256:prompt"}],
                "returned_objects": [
                    {"kind": "assistant_message", "id": "sha256:assistant"},
                    {"kind": "tool_call", "id": "sha256:tool-call"},
                ],
            },
            "idempotency_key": "zeta.model_call.completed:model-1",
            "caused_by": None,
            "session_id": SESSION_ID,
            "turn_id": TURN_ID,
        },
        {
            "event_type": "zeta.tool_call.started",
            "source": "zeta",
            "payload": {
                "tool_call_id": "call-1",
                "status": "pending",
                "name": "read",
                "input": {"path": "README.md"},
                "arguments": '{"path":"README.md"}',
                "_timeline_type": "tool_call",
                "returned_objects": [{"kind": "tool_call", "id": "sha256:tool-call"}],
            },
            "idempotency_key": "zeta.tool_call.started:call-1",
            "caused_by": "model-1",
            "session_id": SESSION_ID,
            "turn_id": TURN_ID,
        },
        {
            "event_type": "zeta.tool_call.completed",
            "source": "zeta",
            "payload": {
                "tool_call_id": "call-1",
                "status": "completed",
                "name": "read",
                "result": {
                    "ok": True,
                    "content": [{"type": "text", "text": "contents"}],
                },
                "capability_id": "sigil.read",
                "model_telemetry": {"input_tokens": 10, "output_tokens": 2},
                "_timeline_type": "tool_result",
                "used_objects": [{"kind": "tool_call", "id": "sha256:tool-call"}],
                "returned_objects": [{"kind": "tool_result", "id": "sha256:result-ok"}],
            },
            "idempotency_key": "zeta.tool_call.completed:result-ok",
            "caused_by": None,
            "session_id": SESSION_ID,
            "turn_id": TURN_ID,
        },
        {
            "event_type": "zeta.tool_call.failed",
            "source": "zeta",
            "payload": {
                "tool_call_id": "call-1",
                "status": "failed",
                "name": "read",
                "result": {
                    "ok": False,
                    "error": {"code": "read_failed", "message": "missing file"},
                },
                "capability_id": "sigil.read",
                "model_telemetry": {"input_tokens": 10, "output_tokens": 2},
                "_timeline_type": "tool_result",
                "used_objects": [{"kind": "tool_call", "id": "sha256:tool-call"}],
                "returned_objects": [
                    {"kind": "tool_result", "id": "sha256:result-failed"}
                ],
            },
            "idempotency_key": "zeta.tool_call.failed:result-failed",
            "caused_by": None,
            "session_id": SESSION_ID,
            "turn_id": TURN_ID,
        },
        {
            "event_type": "zeta.tool_call.failed",
            "source": "zeta",
            "payload": {
                "tool_call_id": "call-1",
                "status": "refused",
                "name": "read",
                "result": {
                    "ok": False,
                    "refusal": {
                        "reason": "capability_not_allowed",
                        "message": "read is not allowed",
                    },
                },
                "capability_id": "sigil.read",
                "model_telemetry": {"input_tokens": 10, "output_tokens": 2},
                "_timeline_type": "tool_result",
                "used_objects": [{"kind": "tool_call", "id": "sha256:tool-call"}],
                "returned_objects": [
                    {"kind": "tool_result", "id": "sha256:result-refused"}
                ],
            },
            "idempotency_key": "zeta.tool_call.failed:result-refused",
            "caused_by": None,
            "session_id": SESSION_ID,
            "turn_id": TURN_ID,
        },
        {
            "event_type": "zeta.turn.failed",
            "source": "zeta",
            "payload": {
                "reason": "max_turns",
                "content": "(turn aborted: max turns)",
                "_timeline_type": "turn_aborted",
            },
            "idempotency_key": None,
            "caused_by": "result-ok",
            "session_id": SESSION_ID,
            "turn_id": TURN_ID,
        },
    ]


def test_zeta_timeline_and_rpc_projection_contract() -> None:
    events = runtime_events()

    assert timeline_from_events(events) == [
        {
            "type": "model",
            "id": "model-1",
            "time": EVENT_TIME + 0.000001,
            "session": SESSION_ID,
            "turn_id": TURN_ID,
            "content": "I will inspect the file.",
            "reasoning": "Need context before editing.",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "read",
                        "arguments": '{"path":"README.md"}',
                    },
                }
            ],
            "used_objects": [{"kind": "prompt", "id": "sha256:prompt"}],
            "returned_objects": [
                {"kind": "assistant_message", "id": "sha256:assistant"},
                {"kind": "tool_call", "id": "sha256:tool-call"},
            ],
            "prompt_trace": {
                "prompt_object_id": "sha256:prompt",
                "assistant_message_object_id": "sha256:assistant",
            },
            "tool_call_object_id": "sha256:tool-call",
        },
        {
            "type": "tool_call",
            "id": "call-1",
            "time": EVENT_TIME + 0.000002,
            "session": SESSION_ID,
            "turn_id": TURN_ID,
            "caused_by": "model-1",
            "tool_call_id": "call-1",
            "status": "pending",
            "name": "read",
            "input": {"path": "README.md"},
            "arguments": '{"path":"README.md"}',
            "returned_objects": [{"kind": "tool_call", "id": "sha256:tool-call"}],
            "tool_call_object_id": "sha256:tool-call",
        },
        {
            "type": "tool_result",
            "id": "result-ok",
            "time": EVENT_TIME + 0.000003,
            "session": SESSION_ID,
            "turn_id": TURN_ID,
            "tool_call_id": "call-1",
            "status": "completed",
            "name": "read",
            "result": {"ok": True, "content": [{"type": "text", "text": "contents"}]},
            "capability_id": "sigil.read",
            "model_telemetry": {"input_tokens": 10, "output_tokens": 2},
            "used_objects": [{"kind": "tool_call", "id": "sha256:tool-call"}],
            "returned_objects": [{"kind": "tool_result", "id": "sha256:result-ok"}],
            "tool_call_object_id": "sha256:tool-call",
            "tool_result_object_id": "sha256:result-ok",
        },
        {
            "type": "tool_result",
            "id": "result-failed",
            "time": EVENT_TIME + 0.000004,
            "session": SESSION_ID,
            "turn_id": TURN_ID,
            "tool_call_id": "call-1",
            "status": "failed",
            "name": "read",
            "result": {
                "ok": False,
                "error": {"code": "read_failed", "message": "missing file"},
            },
            "capability_id": "sigil.read",
            "model_telemetry": {"input_tokens": 10, "output_tokens": 2},
            "used_objects": [{"kind": "tool_call", "id": "sha256:tool-call"}],
            "returned_objects": [{"kind": "tool_result", "id": "sha256:result-failed"}],
            "tool_call_object_id": "sha256:tool-call",
            "tool_result_object_id": "sha256:result-failed",
        },
        {
            "type": "tool_result",
            "id": "result-refused",
            "time": EVENT_TIME + 0.000005,
            "session": SESSION_ID,
            "turn_id": TURN_ID,
            "tool_call_id": "call-1",
            "status": "refused",
            "name": "read",
            "result": {
                "ok": False,
                "refusal": {
                    "reason": "capability_not_allowed",
                    "message": "read is not allowed",
                },
            },
            "capability_id": "sigil.read",
            "model_telemetry": {"input_tokens": 10, "output_tokens": 2},
            "used_objects": [{"kind": "tool_call", "id": "sha256:tool-call"}],
            "returned_objects": [
                {"kind": "tool_result", "id": "sha256:result-refused"}
            ],
            "tool_call_object_id": "sha256:tool-call",
            "tool_result_object_id": "sha256:result-refused",
        },
        {
            "type": "turn_aborted",
            "id": "abort-1",
            "time": EVENT_TIME + 0.000006,
            "session": SESSION_ID,
            "turn_id": TURN_ID,
            "caused_by": "result-ok",
            "reason": "max_turns",
            "content": "(turn aborted: max turns)",
        },
    ]
    assert rpc_event_from_durable_event(events[1]) == {
        **timeline_from_events([events[1]])[0],
        "cursor": "2",
    }
    generic = Event(
        id="external-1",
        event_type="runtime.work.completed",
        source="dispatcher",
        payload={"work_id": "work-1", "result": {"ok": True}},
        idempotency_key=None,
        caused_by="call-1",
        session_id=SESSION_ID,
        turn_id=TURN_ID,
        timestamp_micros=TIMESTAMP_MICROS,
        seq=7,
    )
    assert generic_rpc_event_from_durable_event(generic) == {
        "type": "runtime.work.completed",
        "id": "external-1",
        "source": "dispatcher",
        "time": EVENT_TIME,
        "work_id": "work-1",
        "result": {"ok": True},
        "session": SESSION_ID,
        "turn_id": TURN_ID,
        "caused_by": "call-1",
    }
    assert rpc_event_from_durable_event(generic) == {
        "type": "runtime.work.completed",
        "id": "external-1",
        "source": "dispatcher",
        "time": EVENT_TIME,
        "work_id": "work-1",
        "result": {"ok": True},
        "session": SESSION_ID,
        "turn_id": TURN_ID,
        "caused_by": "call-1",
        "cursor": "7",
    }


def test_zeta_history_record_projection_contract() -> None:
    turn = turn_record(
        TURN_ID,
        workflow="do",
        objective="inspect README",
        contract={"workflow": "do", "allowed_tools": ["read"], "staged": False},
        outcome="executed",
        agent={"model": "test-model", "url": "http://127.0.0.1:8080/v1"},
        cost={"input_tokens": 10, "output_tokens": 2, "model_calls": 1},
        prompt_object_ids=["sha256:prompt"],
        effect_ids=["effect-1"],
    )
    effect = effect_record(
        "effect-1",
        turn_id=TURN_ID,
        kind="file_read",
        staged=False,
        path="README.md",
        tool_call_id="call-1",
        resolved_outcome="completed",
    )
    turn_round_trip = event_from_record(
        {
            **turn,
            "id": "turn-event-1",
            "time": EVENT_TIME,
            "session": SESSION_ID,
            "caused_by": "model-1",
            "cwd": "/repo",
        }
    )
    effect_round_trip = event_from_record(
        {
            **effect,
            "id": "effect-event-1",
            "time": EVENT_TIME,
            "session": SESSION_ID,
            "caused_by": "call-1",
            "cwd": "/repo",
        }
    )

    assert json.dumps(turn, sort_keys=True, separators=(",", ":")) == (
        '{"agent":{"model":"test-model","url":"http://127.0.0.1:8080/v1"},'
        '"contract":{"allowed_tools":["read"],"staged":false,"workflow":"do"},'
        '"cost":{"input_tokens":10,"model_calls":1,"output_tokens":2},'
        '"effect_ids":["effect-1"],"objective":"inspect README",'
        '"outcome":"executed","prompt_object_ids":["sha256:prompt"],'
        '"schema":"zeta.turn","turn_id":"turn-1",'
        '"type":"zeta.turn.completed","workflow":"do"}'
    )
    assert json.dumps(effect, sort_keys=True, separators=(",", ":")) == (
        '{"effect_id":"effect-1","kind":"file_read","path":"README.md",'
        '"resolved_outcome":"completed","schema":"zeta.effect","staged":false,'
        '"tool_call_id":"call-1","turn_id":"turn-1","type":"zeta.effect"}'
    )
    assert turn_round_trip == Event(
        id="turn-event-1",
        event_type="zeta.turn.completed",
        source="zeta",
        payload={
            "schema": "zeta.turn",
            "turn_id": TURN_ID,
            "workflow": "do",
            "objective": "inspect README",
            "contract": {
                "workflow": "do",
                "allowed_tools": ["read"],
                "staged": False,
            },
            "outcome": "executed",
            "prompt_object_ids": ["sha256:prompt"],
            "effect_ids": ["effect-1"],
            "agent": {"model": "test-model", "url": "http://127.0.0.1:8080/v1"},
            "cost": {"input_tokens": 10, "output_tokens": 2, "model_calls": 1},
            "cwd": "/repo",
        },
        idempotency_key=None,
        caused_by="model-1",
        session_id=SESSION_ID,
        turn_id=TURN_ID,
        timestamp_micros=TIMESTAMP_MICROS,
    )
    assert effect_round_trip == Event(
        id="effect-event-1",
        event_type="zeta.effect",
        source="zeta",
        payload={
            "schema": "zeta.effect",
            "effect_id": "effect-1",
            "turn_id": TURN_ID,
            "kind": "file_read",
            "staged": False,
            "path": "README.md",
            "tool_call_id": "call-1",
            "resolved_outcome": "completed",
            "cwd": "/repo",
        },
        idempotency_key=None,
        caused_by="call-1",
        session_id=SESSION_ID,
        turn_id=TURN_ID,
        timestamp_micros=TIMESTAMP_MICROS,
    )
