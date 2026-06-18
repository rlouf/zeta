"""Turn history bookkeeping and trace linkage."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable
from typing import Any

from zeta.capabilities import proposed_effect
from zeta.history import (
    TURN_RECORD_SCHEMA,
    effect_record,
    history_event_record,
    publish_effect_record,
    publish_turn_record,
    turn_record,
)
from zeta.session import Session
from zeta.substrate import (
    Derivation,
    Object,
    PromptTrace,
    add_event_link,
    warn_trace_failure_once,
)

from .protocols import (
    EFFECT_KIND_COMMAND,
    EFFECT_KIND_FILE_EDIT,
    EFFECT_KIND_FILE_WRITE,
    turn_contract,
)
from .sessions import session_id
from .state import event_store_path


class TurnRecorder:
    """Accumulate one agent turn's history facts and append its records.

    Effects are attached to the matching tool result before it is persisted as
    a Zeta tool call event; ``finish`` appends the turn record referencing
    them.
    """

    def __init__(
        self,
        *,
        runtime_context: Session,
        workflow: str,
        objective: str,
        allowed_tools: Iterable[str],
        staged: bool,
        agent: dict[str, str] | None = None,
    ) -> None:
        self.runtime_context = runtime_context
        self.turn_id = str(uuid.uuid4())
        self.workflow = workflow
        self.objective = objective
        self.contract = turn_contract(workflow, allowed_tools, staged=staged)
        self.agent = agent
        self.started = time.monotonic()
        self.effect_ids: list[str] = []
        self.effects: list[dict[str, Any]] = []
        self.effect_object_ids: list[str] = []
        self.model_calls: list[dict[str, Any]] = []
        self.root_event_id: str | None = None
        self.last_runtime_event_id: str | None = None

    def note_root_event(self, event: dict[str, Any]) -> None:
        event_id = event_id_value(event)
        if event_id is not None:
            self.root_event_id = event_id

    def note_runtime_event(self, event: dict[str, Any]) -> None:
        event_id = event_id_value(event)
        if event_id is None or not is_durable_runtime_event(event):
            return
        self.last_runtime_event_id = event_id

    def causal_parent_event_id(self) -> str | None:
        return self.last_runtime_event_id or self.root_event_id

    def attach_tool_result_effect(self, event: dict[str, Any]) -> None:
        """Attach the effect record a tool result implies, if any."""
        fields = tool_result_effect_fields(
            str(event.get("name") or ""),
            event.get("result"),
        )
        if fields is None:
            return
        effect_id = str(uuid.uuid4())
        tool_call_id = str(event.get("tool_call_id") or "")
        payload = effect_record(
            effect_id,
            turn_id=self.turn_id,
            tool_call_id=tool_call_id or None,
            **fields,
        )
        event["effects"] = [*event.get("effects", []), payload]
        payload = publish_effect_record(
            payload,
            path=event_store_path(),
            session_id=session_id(),
        )
        self.effect_ids.append(effect_id)
        self.effects.append(payload)
        object_id = str(event.get("tool_result_object_id") or "")
        if object_id:
            self.effect_object_ids.append(object_id)

    def add_model_calls(self, calls: Iterable[dict[str, Any]]) -> None:
        self.model_calls.extend(call for call in calls if call)

    def finish(
        self,
        outcome: str,
        prompt_traces: Iterable[PromptTrace] = (),
    ) -> dict[str, Any]:
        """Append the turn record closing this turn and bridge it into the graph."""
        record = turn_record(
            self.turn_id,
            workflow=self.workflow,
            objective=self.objective,
            contract=self.contract,
            outcome=outcome,
            agent=self.agent,
            cost=self.cost(),
            prompt_object_ids=[trace.prompt_object_id for trace in prompt_traces],
            effect_ids=self.effect_ids,
        )
        caused_by = self.causal_parent_event_id()
        if caused_by is not None:
            record["caused_by"] = caused_by
        event = publish_turn_record(
            record,
            path=event_store_path(),
            session_id=session_id(),
        )
        payload = history_event_record(event)
        record_turn_trace_object(
            payload,
            self.effects,
            self.effect_object_ids,
            runtime_context=self.runtime_context,
        )
        return payload

    def cost(self) -> dict[str, int]:
        wall_ms = int((time.monotonic() - self.started) * 1000)
        if not self.model_calls:
            return {"wall_ms": wall_ms}
        input_tokens = 0
        output_tokens = 0
        for call in self.model_calls:
            usage = call.get("usage")
            if not isinstance(usage, dict):
                continue
            input_tokens += usage_tokens(usage, "prompt_tokens")
            output_tokens += usage_tokens(usage, "completion_tokens")
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "model_calls": len(self.model_calls),
            "wall_ms": wall_ms,
        }


def record_turn_trace_object(
    payload: dict[str, Any],
    effects: list[dict[str, Any]],
    effect_object_ids: list[str],
    *,
    runtime_context: Session,
) -> None:
    """Bridge one turn record into the session trace graph, fail-open.

    The turn object links the prompts the model saw and the tool results
    that evidence its effects, so `graph_closure` walks objective →
    prompt(s) → components → tool results in one pass, and the
    `turn/<turn_id>` ref makes turn ids resolve like trace ids.
    """
    try:
        store = runtime_context.trace_store
        prompt_ids = payload.get("prompt_object_ids")
        links: list[str] = []
        for object_id in [
            *(prompt_ids if isinstance(prompt_ids, list) else []),
            *effect_object_ids,
        ]:
            add_event_link(links, object_id)
        with store.batch():
            turn_object_id = store.put_object(
                Object(
                    kind=TURN_RECORD_SCHEMA,
                    schema=TURN_RECORD_SCHEMA,
                    data={**payload, "effects": effects},
                    links=tuple(links),
                )
            )
            store.record_derivation(
                Derivation(
                    producer="TurnRecord",
                    output_id=turn_object_id,
                    input_ids=tuple(links),
                    params={
                        "workflow": str(payload.get("workflow") or ""),
                        "outcome": str(payload.get("outcome") or ""),
                    },
                )
            )
            store.set_ref(f"turn/{payload.get('turn_id')}", turn_object_id)
    except Exception as exc:
        warn_trace_failure_once("record_turn_trace_object", exc)


def usage_tokens(usage: dict[str, Any], field_name: str) -> int:
    value = usage.get(field_name)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def tool_result_effect_fields(name: str, result: Any) -> dict[str, Any] | None:
    """Map one tool result onto history effect fields, or None for no effect."""
    if not isinstance(result, dict):
        return None
    metadata = result.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    staged_effect = proposed_effect(result)
    staged = staged_effect is not None
    if name in {"write", "edit"}:
        return file_effect_fields(name, result, metadata, staged=staged)
    if name == "bash":
        return command_effect_fields(result, metadata, staged_effect=staged_effect)
    return None


def event_id_value(event: dict[str, Any]) -> str | None:
    event_id = event.get("id")
    return event_id if isinstance(event_id, str) and event_id else None


def is_durable_runtime_event(event: dict[str, Any]) -> bool:
    event_type = str(event.get("type") or "")
    if event_type in {"model", "turn_aborted"}:
        return True
    if event_type != "tool_result":
        return False
    return (
        "effects" in event
        or "returned_objects" in event
        or bool(event.get("tool_result_object_id"))
    )


def file_effect_fields(
    name: str,
    result: dict[str, Any],
    metadata: dict[str, Any],
    *,
    staged: bool,
) -> dict[str, Any] | None:
    if not (staged or result.get("ok") is True):
        return None
    path = metadata.get("path") or metadata.get("location")
    if not isinstance(path, str) or not path:
        return None
    fields: dict[str, Any] = {
        "kind": EFFECT_KIND_FILE_WRITE if name == "write" else EFFECT_KIND_FILE_EDIT,
        "staged": staged,
        "path": path,
    }
    for key in ("before_hash", "after_hash"):
        value = metadata.get(key)
        if isinstance(value, str):
            fields[key] = value
    return fields


def command_effect_fields(
    result: dict[str, Any],
    metadata: dict[str, Any],
    *,
    staged_effect: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if staged_effect is not None:
        return {
            "kind": EFFECT_KIND_COMMAND,
            "staged": True,
            "command": str(staged_effect.get("command") or ""),
        }
    status = metadata.get("status")
    if not isinstance(status, int) or isinstance(status, bool):
        return None
    fields: dict[str, Any] = {
        "kind": EFFECT_KIND_COMMAND,
        "staged": False,
        "command": str(metadata.get("command") or ""),
        "exit_status": status,
    }
    duration = metadata.get("duration_ms")
    if isinstance(duration, int) and not isinstance(duration, bool):
        fields["duration_ms"] = duration
    return fields
