"""Object-level provenance projected from durable events."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from zeta.records.events import DraftEvent, Event, draft_event_id
from zeta.records.objects import Derivation, Object, ObjectId
from zeta.records.stores import Store


@dataclass(frozen=True)
class TraceProjection:
    """Trace ids derived from replaying domain events."""

    prompt_object_ids: dict[str, ObjectId]
    assistant_message_ids: dict[str, ObjectId]
    tool_call_object_ids: dict[str, ObjectId]
    tool_result_object_ids: dict[str, ObjectId]


def project_trace_events(
    events: Iterable[Event], store: Store | None
) -> TraceProjection:
    projection = TraceProjection({}, {}, {}, {})
    if store is None:
        return projection
    latest_assistant_id: ObjectId | None = None
    for event in events:
        timeline_type = event_timeline_type(event)
        if timeline_type == "model":
            latest_assistant_id = project_model_event(event, store, projection)
            continue
        if timeline_type == "tool_call":
            project_tool_call_event(
                event,
                store,
                projection,
                latest_assistant_id=latest_assistant_id,
            )
            continue
        if timeline_type == "tool_result":
            project_tool_result_event(event, store, projection)
    return projection


def project_trace_drafts(
    drafts: Iterable[DraftEvent],
    store: Store | None,
) -> TraceProjection:
    return project_trace_events(
        (
            Event(
                id=draft_event_id(draft) or "",
                event_type=draft.event_type,
                source=draft.source,
                payload=draft.payload,
                idempotency_key=draft.idempotency_key,
                caused_by=draft.caused_by,
                session_id=draft.session_id,
                run_id=draft.run_id,
                turn_id=draft.turn_id,
                timestamp_ms=0,
            )
            for draft in drafts
        ),
        store,
    )


def event_timeline_type(event: Event) -> str:
    view_type = event.payload.get("_timeline_type")
    if isinstance(view_type, str) and view_type:
        return view_type
    prefix = "zeta."
    if event.event_type.startswith(prefix):
        return event.event_type[len(prefix) :]
    return event.event_type


def project_model_event(
    event: Event,
    store: Store,
    projection: TraceProjection,
) -> ObjectId | None:
    prompt_id = event.payload.get("prompt_object_id")
    if not isinstance(prompt_id, str) or not prompt_id.startswith("sha256:"):
        return None
    assistant_id = store.put_object(
        Object(
            kind="assistant_message",
            schema="zeta.model_output.v1",
            data=model_trace_data(event),
            links=(prompt_id,),
        )
    )
    store.record_derivation(
        Derivation(
            producer="ModelResponse",
            output_id=assistant_id,
            input_ids=(prompt_id,),
            params={},
        )
    )
    projection.prompt_object_ids[event.id] = prompt_id
    projection.assistant_message_ids[event.id] = assistant_id
    return assistant_id


def project_tool_call_event(
    event: Event,
    store: Store,
    projection: TraceProjection,
    *,
    latest_assistant_id: ObjectId | None,
) -> ObjectId | None:
    source_id = (
        projection.assistant_message_ids.get(event.caused_by or "")
        or latest_assistant_id
    )
    if source_id is None:
        return None
    payload = dict(event.payload)
    call_id = store.put_object(
        Object(
            kind="tool_call",
            schema="zeta.tool_call.v1",
            data=tool_call_object_data(payload),
            links=(source_id,),
        )
    )
    store.record_derivation(
        Derivation(
            producer="ToolCallProjection",
            output_id=call_id,
            input_ids=(source_id,),
            params=tool_event_derivation_params(payload),
        )
    )
    projection.tool_call_object_ids[event.id] = call_id
    tool_call_id = payload.get("tool_call_id")
    if isinstance(tool_call_id, str) and tool_call_id:
        projection.tool_call_object_ids[tool_call_id] = call_id
    return call_id


def project_tool_result_event(
    event: Event,
    store: Store,
    projection: TraceProjection,
) -> ObjectId | None:
    payload = dict(event.payload)
    tool_call_id = payload.get("tool_call_id")
    call_object_id = (
        projection.tool_call_object_ids.get(tool_call_id)
        if isinstance(tool_call_id, str)
        else None
    )
    if call_object_id is None:
        return None
    result_id = store.put_object(
        Object(
            kind="tool_result",
            schema="zeta.tool_result.v1",
            data=tool_result_object_data(payload),
            links=(call_object_id,),
        )
    )
    store.record_derivation(
        Derivation(
            producer="ToolExecution",
            output_id=result_id,
            input_ids=(call_object_id,),
            params=tool_event_derivation_params(payload),
        )
    )
    projection.tool_result_object_ids[event.id] = result_id
    return result_id


def model_trace_data(event: Event) -> dict[str, Any]:
    message: dict[str, Any] = {}
    content = event.payload.get("content")
    if isinstance(content, str):
        message["content"] = content
    reasoning = event.payload.get("reasoning")
    if isinstance(reasoning, str):
        message["reasoning_content"] = reasoning
    tool_calls = event.payload.get("tool_calls")
    if isinstance(tool_calls, list):
        message["tool_calls"] = [call for call in tool_calls if isinstance(call, dict)]
    return {"message": dict(message), "model_output": {"message": dict(message)}}


def tool_call_object_data(event: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tool_call_id": str(event.get("tool_call_id") or event.get("id") or ""),
        "name": str(event.get("name") or ""),
        "input": event.get("input") if isinstance(event.get("input"), dict) else {},
    }
    arguments = event.get("arguments")
    if isinstance(arguments, str):
        data["arguments"] = arguments
    return data


def tool_result_object_data(event: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tool_call_id": str(event.get("tool_call_id") or ""),
        "name": str(event.get("name") or ""),
    }
    result = event.get("result")
    if isinstance(result, dict):
        data["result"] = result
    model_telemetry = event.get("model_telemetry")
    if isinstance(model_telemetry, dict):
        data["model_telemetry"] = model_telemetry
    return data


def tool_event_derivation_params(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool_call_id": str(event.get("tool_call_id") or event.get("id") or ""),
        "name": str(event.get("name") or ""),
    }
