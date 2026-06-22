import json
from dataclasses import asdict, replace
from typing import Any

from zeta.context.builder import project_trace_events
from zeta.records.events import (
    DraftEvent,
    Event,
    durable_model_event_payload,
    durable_tool_event_payload,
    event_view,
    model_call_draft,
    runtime_event_draft,
    tool_call_draft,
)
from zeta.records.objects import Derivation, Object
from zeta.records.stores import InMemoryStore
from zeta.records.timeline import effect_record, event_from_record, turn_record

SESSION_ID = "session-1"
TURN_ID = "turn-1"
TIMESTAMP_MS = 1_700_000_000_123
EVENT_TIME = 1_700_000_000.123


def timestamp_time(cursor: int = 0) -> float:
    return (TIMESTAMP_MS + cursor) / 1_000


def event_from_draft(
    draft: DraftEvent,
    *,
    event_id: str,
    cursor: int,
) -> Event:
    return replace(
        Event.from_draft(draft),
        id=event_id,
        timestamp_ms=TIMESTAMP_MS + cursor,
        cursor=cursor,
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
        model_call_draft(
            payload=durable_model_event_payload(model),
            turn_id=TURN_ID,
            session_id=SESSION_ID,
            event_id="model-1",
        ),
        tool_call_draft(
            payload=durable_tool_event_payload(call),
            turn_id=TURN_ID,
            session_id=SESSION_ID,
            caused_by="model-1",
            event_id="call-1",
        ),
        tool_call_draft(
            payload=durable_tool_event_payload(result_ok),
            turn_id=TURN_ID,
            session_id=SESSION_ID,
            event_id="result-ok",
        ),
        tool_call_draft(
            payload=durable_tool_event_payload(result_failed),
            turn_id=TURN_ID,
            session_id=SESSION_ID,
            event_id="result-failed",
        ),
        tool_call_draft(
            payload=durable_tool_event_payload(result_refused),
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
        event_from_draft(draft, event_id=event_id, cursor=index + 1)
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
        drafts.append(
            runtime_event_draft(event, session_id=SESSION_ID, turn_id=TURN_ID)
        )

    assert [asdict(draft) for draft in drafts] == [
        asdict(draft) for draft in runtime_drafts()
    ]


def test_zeta_runtime_event_draft_handles_special_and_generic_events() -> None:
    generic = {
        "type": "custom.event",
        "id": "custom-1",
        "content": "kept",
    }

    drafts = [
        runtime_event_draft(model_event(), session_id=SESSION_ID, turn_id=TURN_ID),
        runtime_event_draft(tool_call_event(), session_id=SESSION_ID, turn_id=TURN_ID),
        runtime_event_draft(
            turn_aborted_event(),
            session_id=SESSION_ID,
            turn_id=TURN_ID,
        ),
        runtime_event_draft(generic, session_id=SESSION_ID, turn_id=TURN_ID),
    ]

    assert [draft.event_type for draft in drafts] == [
        "zeta.model_call.completed",
        "zeta.tool_call.started",
        "zeta.turn.failed",
        "custom.event",
    ]
    assert drafts[-1].payload == {"content": "kept"}


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
            },
            "idempotency_key": "zeta.model_call.completed:model-1",
            "caused_by": None,
            "session_id": SESSION_ID,
            "run_id": None,
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
            },
            "idempotency_key": "zeta.tool_call.started:call-1",
            "caused_by": "model-1",
            "session_id": SESSION_ID,
            "run_id": None,
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
            },
            "idempotency_key": "zeta.tool_call.completed:result-ok",
            "caused_by": None,
            "session_id": SESSION_ID,
            "run_id": None,
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
            },
            "idempotency_key": "zeta.tool_call.failed:result-failed",
            "caused_by": None,
            "session_id": SESSION_ID,
            "run_id": None,
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
            },
            "idempotency_key": "zeta.tool_call.failed:result-refused",
            "caused_by": None,
            "session_id": SESSION_ID,
            "run_id": None,
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
            "run_id": None,
            "turn_id": TURN_ID,
        },
    ]


def test_zeta_event_view_and_rpc_projection_contract() -> None:
    events = runtime_events()
    projected = [event_view(event) for event in events]

    assert [event["cursor"] for event in projected] == ["1", "2", "3", "4", "5", "6"]
    assert [
        {key: value for key, value in event.items() if key != "cursor"}
        for event in projected
    ] == [
        {
            "type": "model",
            "id": "model-1",
            "time": timestamp_time(1),
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
        },
        {
            "type": "tool_call",
            "id": "call-1",
            "time": timestamp_time(2),
            "session": SESSION_ID,
            "turn_id": TURN_ID,
            "caused_by": "model-1",
            "tool_call_id": "call-1",
            "status": "pending",
            "name": "read",
            "input": {"path": "README.md"},
            "arguments": '{"path":"README.md"}',
        },
        {
            "type": "tool_result",
            "id": "result-ok",
            "time": timestamp_time(3),
            "session": SESSION_ID,
            "turn_id": TURN_ID,
            "tool_call_id": "call-1",
            "status": "completed",
            "name": "read",
            "result": {"ok": True, "content": [{"type": "text", "text": "contents"}]},
            "capability_id": "sigil.read",
            "model_telemetry": {"input_tokens": 10, "output_tokens": 2},
        },
        {
            "type": "tool_result",
            "id": "result-failed",
            "time": timestamp_time(4),
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
        },
        {
            "type": "tool_result",
            "id": "result-refused",
            "time": timestamp_time(5),
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
        },
        {
            "type": "turn_aborted",
            "id": "abort-1",
            "time": timestamp_time(6),
            "session": SESSION_ID,
            "turn_id": TURN_ID,
            "caused_by": "result-ok",
            "reason": "max_turns",
            "content": "(turn aborted: max turns)",
        },
    ]
    assert event_view(events[1])["cursor"] == "2"
    generic = Event(
        id="external-1",
        event_type="runtime.queue_item.completed",
        source="dispatcher",
        payload={"queue_item_id": "qi-1", "result": {"ok": True}},
        idempotency_key=None,
        caused_by="call-1",
        session_id=SESSION_ID,
        turn_id=TURN_ID,
        timestamp_ms=TIMESTAMP_MS,
        cursor=7,
    )
    assert event_view(generic) == {
        "type": "runtime.queue_item.completed",
        "id": "external-1",
        "source": "dispatcher",
        "time": timestamp_time(),
        "queue_item_id": "qi-1",
        "result": {"ok": True},
        "session": SESSION_ID,
        "turn_id": TURN_ID,
        "caused_by": "call-1",
        "cursor": "7",
    }


def test_zeta_pure_runtime_events_project_to_trace_graph() -> None:
    store = InMemoryStore()
    prompt_id = store.put_object(
        Object(
            kind="prompt",
            schema="zeta.prompt.v1",
            data={"payload_sha256": "test"},
        )
    )
    model = event_from_draft(
        model_call_draft(
            payload={
                "_timeline_type": "model",
                "content": "I will inspect the file.",
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
                "prompt_object_id": prompt_id,
            },
            turn_id=TURN_ID,
            session_id=SESSION_ID,
            event_id="model-1",
        ),
        event_id="model-1",
        cursor=1,
    )
    tool_call = event_from_draft(
        tool_call_draft(
            payload={
                "_timeline_type": "tool_call",
                "tool_call_id": "call-1",
                "status": "pending",
                "name": "read",
                "input": {"path": "README.md"},
                "arguments": '{"path":"README.md"}',
            },
            turn_id=TURN_ID,
            session_id=SESSION_ID,
            caused_by="model-1",
            event_id="call-1",
        ),
        event_id="call-1",
        cursor=2,
    )
    tool_result = event_from_draft(
        tool_call_draft(
            payload={
                "_timeline_type": "tool_result",
                "tool_call_id": "call-1",
                "status": "completed",
                "name": "read",
                "result": {
                    "ok": True,
                    "content": [{"type": "text", "text": "contents"}],
                },
                "capability_id": "sigil.read",
                "model_telemetry": {"input_tokens": 10, "output_tokens": 2},
            },
            turn_id=TURN_ID,
            session_id=SESSION_ID,
            event_id="result-ok",
        ),
        event_id="result-ok",
        cursor=3,
    )

    projection = project_trace_events([model, tool_call, tool_result], store)
    replayed = project_trace_events([model, tool_call, tool_result], store)

    assistant_id = projection.assistant_message_ids["model-1"]
    tool_call_id = projection.tool_call_object_ids["call-1"]
    tool_result_id = projection.tool_result_object_ids["result-ok"]
    assert replayed == projection
    assert store.get_object(assistant_id) == Object(
        kind="assistant_message",
        schema="zeta.model_output.v1",
        data={
            "message": {
                "content": "I will inspect the file.",
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
            },
            "model_output": {
                "message": {
                    "content": "I will inspect the file.",
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
                }
            },
        },
        links=(prompt_id,),
    )
    assert store.get_object(tool_call_id) == Object(
        kind="tool_call",
        schema="zeta.tool_call.v1",
        data={
            "tool_call_id": "call-1",
            "name": "read",
            "input": {"path": "README.md"},
            "arguments": '{"path":"README.md"}',
        },
        links=(assistant_id,),
    )
    assert store.get_object(tool_result_id) == Object(
        kind="tool_result",
        schema="zeta.tool_result.v1",
        data={
            "tool_call_id": "call-1",
            "name": "read",
            "result": {
                "ok": True,
                "content": [{"type": "text", "text": "contents"}],
            },
            "model_telemetry": {"input_tokens": 10, "output_tokens": 2},
        },
        links=(tool_call_id,),
    )
    assert store.derivations_for_output(assistant_id) == [
        Derivation(
            producer="ModelResponse",
            output_id=assistant_id,
            input_ids=(prompt_id,),
            params={},
        )
    ]
    assert store.derivations_for_output(tool_call_id) == [
        Derivation(
            producer="ToolCallProjection",
            output_id=tool_call_id,
            input_ids=(assistant_id,),
            params={"tool_call_id": "call-1", "name": "read"},
        )
    ]
    assert store.derivations_for_output(tool_result_id) == [
        Derivation(
            producer="ToolExecution",
            output_id=tool_result_id,
            input_ids=(tool_call_id,),
            params={"tool_call_id": "call-1", "name": "read"},
        )
    ]


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
        timestamp_ms=TIMESTAMP_MS,
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
        timestamp_ms=TIMESTAMP_MS,
    )
