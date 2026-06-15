"""Shared I/O plumbing for Zeta-backed agent workflows.

Both workflows persist agent events to the Zeta run timeline, render tool traces
and a context-usage footer while the loop runs, and replay any events the
recorder missed. This module owns that skeleton; workflow modules own
workflow-specific tagging, logging, and handoff handling.
"""

from __future__ import annotations

import sys
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, TextIO

from zeta.agent import AgentConfig, AgentTurnResult, registered_tools, run_agent_turn
from zeta.context import ZetaContext, load_project_context
from zeta.models import (
    CODEX_RESPONSES_API,
    ModelSelection,
    active_model_selection,
    ensure_server,
    model_selection_event,
)
from zeta.timeline import add_event_link, current_timeline, record_event
from zeta.tools.base import proposed_effect
from zeta.tools.registry import ExecutionMode
from zeta.trace import (
    Derivation,
    Object,
    PromptTrace,
    warn_trace_failure_once,
)

from .display.render import (
    PROGRESS_MODE_TRACE,
    ContextUsageFooter,
    TerminalDigestRenderer,
    TraceAwareStreamRenderer,
    TraceRenderState,
    create_stream_renderer,
    progress_mode_from_env,
    render_tool_start,
)
from .ledger import append_effect_record, append_turn_record, ledger_event_record
from .protocols import (
    EFFECT_KIND_COMMAND,
    EFFECT_KIND_FILE_EDIT,
    EFFECT_KIND_FILE_WRITE,
    TURN_OUTCOME_ABORTED,
    TURN_OUTCOME_ANSWERED,
    TURN_OUTCOME_EXECUTED,
    TURN_OUTCOME_FAILED,
    TURN_OUTCOME_STAGED,
    TURN_RECORD_SCHEMA,
    effect_record,
    turn_contract,
    turn_record,
)
from .session import session_id
from .state import append_prompt_submitted_event
from .tools import ensure_builtin_tools_registered


def model_server_ready(selected_model: ModelSelection | None) -> bool:
    """Check endpoint reachability for the active model selection."""
    if selected_model is not None and selected_model.api == CODEX_RESPONSES_API:
        return True
    if selected_model is not None:
        return ensure_server(
            selected_url=selected_model.url,
            selected_model=selected_model.model,
        )
    return ensure_server()


@dataclass(frozen=True)
class TurnRenderer:
    """Rendering state shared across one workflow turn."""

    trace_state: TraceRenderState
    context_footer: ContextUsageFooter | None
    stream_renderer: TraceAwareStreamRenderer | None
    progress_renderer: TerminalDigestRenderer | None


def build_turn_renderer(
    footer_output: TextIO,
    *,
    objective: str = "",
) -> TurnRenderer:
    """Build the trace state, context footer, and stream renderer for a turn."""
    trace_state = TraceRenderState()
    context_footer = ContextUsageFooter(footer_output)
    progress_mode = progress_mode_from_env()
    progress_renderer = TerminalDigestRenderer(
        footer_output,
        mode=progress_mode,
        objective=objective,
    )
    base_stream_renderer = create_stream_renderer(sys.stdout)
    stream_renderer = TraceAwareStreamRenderer(
        base_stream_renderer,
        trace_state,
        sys.stdout,
        before_output=context_footer.clear,
    )
    return TurnRenderer(trace_state, context_footer, stream_renderer, progress_renderer)


class TurnLedger:
    """Accumulate one agent turn's ledger facts and append its records.

    Effects are attached to the matching tool result before it is persisted as
    a Zeta tool call event; ``finish`` appends the turn record referencing
    them.
    """

    def __init__(
        self,
        *,
        runtime_context: ZetaContext,
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
        payload = append_effect_record(payload)
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
        event = append_turn_record(record)
        payload = ledger_event_record(event)
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
    runtime_context: ZetaContext,
) -> None:
    """Bridge one turn record into the session trace graph, fail-open.

    The turn object links the prompts the model saw and the tool results
    that evidence its effects, so `graph_closure` walks objective →
    prompt(s) → components → tool results in one pass, and the
    `turn/<turn_id>` ref makes ledger ids resolve like trace ids.
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
    """Map one tool result onto ledger effect fields, or None for no effect."""
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
    if event_type == "model":
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


class TurnEventRecorder:
    """Persist and render agent events as the loop produces them.

    Subclasses set ``tag_fields``/``strip_fields`` for timeline tagging and
    override ``handle_tool_call``/``handle_tool_result`` for workflow behavior.
    ``handle_tool_result`` may return an exit status; the last one wins.
    A ledger, when given, tags every persisted event with the turn id and
    receives tool results for effect recording.
    """

    tag_fields: dict[str, Any] = {}
    strip_fields: frozenset[str] = frozenset()

    def __init__(
        self,
        renderer: TurnRenderer,
        *,
        render_output: TextIO,
        ledger: TurnLedger | None = None,
        runtime_context: ZetaContext,
    ) -> None:
        self.renderer = renderer
        self.render_output = render_output
        self.ledger = ledger
        self.runtime_context = runtime_context
        self.recorded_event_ids: set[int] = set()
        self.status: int | None = None

    def record(self, event: dict[str, Any]) -> None:
        self.recorded_event_ids.add(id(event))
        self.record_event(event)

    def replay(self, result: AgentTurnResult) -> None:
        """Record any turn events the live sink did not see."""
        for event in result.events:
            if id(event) in self.recorded_event_ids:
                continue
            self.record_event(event)

    def record_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "tool_result" and self.ledger is not None:
            self.ledger.attach_tool_result_effect(event)
        persisted = self.persist(event_type, event)
        if self.ledger is not None:
            self.ledger.note_runtime_event(persisted)
        if event_type == "tool_call":
            params = persisted.get("input")
            self.handle_tool_call(
                str(persisted.get("name") or ""),
                params if isinstance(params, dict) else {},
            )
            return
        if event_type != "tool_result":
            return
        status = self.handle_tool_result(persisted)
        if status is not None:
            self.status = status

    def persist(self, event_type: str, event: dict[str, Any]) -> dict[str, Any]:
        fields = {
            key: value
            for key, value in event.items()
            if key != "type" and key not in self.strip_fields
        }
        if self.ledger is not None:
            fields["turn_id"] = self.ledger.turn_id
        return record_zeta_event(
            event_type,
            runtime_context=self.runtime_context,
            **fields,
            **self.tag_fields,
        )

    def handle_tool_call(self, name: str, args: dict[str, Any]) -> None:
        self.render_tool_call(name, args)

    def render_tool_call(self, name: str, args: dict[str, Any]) -> None:
        if (
            self.renderer.progress_renderer is not None
            and self.renderer.progress_renderer.mode != PROGRESS_MODE_TRACE
        ):
            self.renderer.progress_renderer.observe_tool_call(name, args)
            return
        if self.renderer.context_footer is not None:
            self.renderer.context_footer.clear()
        if self.renderer.stream_renderer is not None:
            self.renderer.stream_renderer.ensure_trace_boundary()
        render_tool_start(name, args, output=self.render_output, newline=False)

    def handle_tool_result(self, persisted: dict[str, Any]) -> int | None:
        raise NotImplementedError


def render_final_text(
    content: str,
    *,
    streamed: bool,
    renderer: TurnRenderer,
) -> None:
    """Print a buffered final answer, or close out an already-streamed one."""
    if not content:
        return
    if streamed:
        if renderer.stream_renderer is not None:
            renderer.stream_renderer.finish()
        return
    if not renderer.trace_state.render_text_separator(sys.stdout):
        print()
    print(content)
    print()


def record_zeta_event(
    event_type: str,
    *,
    runtime_context: ZetaContext,
    **fields: Any,
) -> dict[str, Any]:
    return record_event({"type": event_type, **fields}, runtime_context=runtime_context)


def record_turn_abort(
    error: BaseException,
    *,
    runtime_context: ZetaContext,
    **fields: Any,
) -> dict[str, Any]:
    """Resolve an aborted turn in the timeline instead of dangling its question."""
    return record_zeta_event(
        "turn_aborted",
        runtime_context=runtime_context,
        error=str(error),
        content=f"(turn aborted: {error})",
        **fields,
    )


def run_zeta_rpc_session(
    params: dict[str, Any],
    *,
    publish_event: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    from . import zeta_context_for_sigil

    runtime_context = zeta_context_for_sigil()
    objective = str(params.get("objective") or "")
    if not objective:
        raise ValueError("session.run requires objective")
    workflow = str(params.get("workflow") or "propose")
    if workflow not in {"ask", "propose", "do"}:
        raise ValueError("workflow must be ask, propose, or do")
    requested_tools = params.get("tools")
    allowed_tools = (
        tuple(str(tool) for tool in requested_tools if isinstance(tool, str))
        if isinstance(requested_tools, list)
        else None
    )
    ensure_builtin_tools_registered()
    selected_model = active_model_selection(session_dir=runtime_context.session_dir)
    enabled_tools = registered_tools(
        allowed_tools,
        tool_registry=runtime_context.tool_registry,
    )
    execution_mode: ExecutionMode = "direct" if workflow == "do" else "stage"
    ledger = TurnLedger(
        runtime_context=runtime_context,
        workflow=workflow,
        objective=objective,
        allowed_tools=enabled_tools,
        staged=any(
            tool.spec.mutates()
            for name in enabled_tools
            if (tool := runtime_context.tool_registry.get(name)) is not None
        )
        and execution_mode == "stage",
        agent=model_selection_event(selected_model) if selected_model else None,
    )
    prior_timeline = current_timeline(runtime_context=runtime_context)
    user_event = record_event(
        {
            "type": "user_message",
            "content": objective,
            "workflow": workflow,
            "runtime": "zeta-rpc",
            "turn_id": ledger.turn_id,
            "available_tools": list(enabled_tools),
        },
        runtime_context=runtime_context,
    )
    ledger.note_root_event(user_event)
    append_prompt_submitted_event(user_event)
    publish_event(user_event)

    def sink(event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "tool_result":
            ledger.attach_tool_result_effect(event)
        event["turn_id"] = ledger.turn_id
        persisted = record_event(event, runtime_context=runtime_context)
        ledger.note_runtime_event(persisted)
        publish_event(persisted)

    try:
        result = run_agent_turn(
            objective,
            prior_timeline,
            AgentConfig(
                system_prompt=params.get("system")
                if isinstance(params.get("system"), str)
                else None,
                allowed_tools=enabled_tools,
                max_turns=params.get("max_steps")
                if isinstance(params.get("max_steps"), int)
                else None,
                stop_on_staged_effect=True,
                execution_mode=execution_mode,
                model_profile=selected_model.profile
                if selected_model is not None
                else None,
                model_name=selected_model.model if selected_model is not None else None,
                model_url=selected_model.url if selected_model is not None else None,
                model_session_id=runtime_context.session_id,
                thinking=selected_model.thinking
                if selected_model is not None
                else None,
                model_api=selected_model.api if selected_model is not None else None,
            ),
            context=(
                str(params.get("context"))
                if isinstance(params.get("context"), str)
                else load_project_context()
            ),
            event_sink=sink,
            trace_store=runtime_context.trace_store,
            tool_registry=runtime_context.tool_registry,
            caused_by=ledger.root_event_id,
        )
    except RuntimeError as error:
        record_turn_abort(
            error,
            runtime_context=runtime_context,
            workflow=workflow,
            caused_by=ledger.causal_parent_event_id(),
        )
        turn = ledger.finish(TURN_OUTCOME_ABORTED)
        publish_event(turn)
        raise
    ledger.add_model_calls(result.model_telemetry_calls)
    if result.staged_effect is not None:
        outcome = TURN_OUTCOME_STAGED
    elif result.final_text:
        outcome = TURN_OUTCOME_EXECUTED if ledger.effect_ids else TURN_OUTCOME_ANSWERED
    else:
        outcome = TURN_OUTCOME_FAILED
    turn = ledger.finish(outcome, prompt_traces=result.prompt_traces)
    publish_event(turn)
    return {
        "turn_id": ledger.turn_id,
        "session_id": session_id(),
        "outcome": outcome,
        "final_text": result.final_text,
    }


def model_telemetry_fields(
    model_telemetry: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(model_telemetry, dict):
        return {}
    fields: dict[str, Any] = {}
    usage = model_telemetry.get("usage")
    if isinstance(usage, dict):
        fields["usage"] = usage
    context_tokens = model_telemetry.get("model_context_tokens")
    if isinstance(context_tokens, int) and not isinstance(context_tokens, bool):
        fields["model_context_tokens"] = context_tokens
    return fields


def event_model_telemetry(event: dict[str, Any]) -> dict[str, Any] | None:
    model_telemetry = event.get("model_telemetry")
    if isinstance(model_telemetry, dict):
        return model_telemetry
    return None
