"""Read-only shell ask workflow.

Discussion continuity comes from the session timeline: every ask turn reads
the prior timeline and records its own events, so asks remember exactly what
the other glyphs remember. A new shell session starts a fresh thread.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterable
from types import TracebackType
from typing import Any

from ..agent_io import (
    TurnEventRecorder,
    TurnRenderer,
    build_turn_renderer,
    event_model_telemetry,
    model_server_ready,
    model_telemetry_fields,
    record_turn_abort,
    render_final_text,
)
from ..display.render import (
    StreamRenderer,
    ThinkingStatus,
    render_tool_result_summary,
    thinking_status_factory,
)
from ..session import active_failure_context, recent_turns_context
from ..state import (
    append_event,
    append_jsonl,
    write_jsonl,
)
from ..zeta.agent import AgentConfig, run_agent_turn
from ..zeta.context import load_project_context
from ..zeta.model import ChatCompletionStreamSink, chat_text
from ..zeta.models import ModelSelection, active_model_selection, model_selection_event
from ..zeta.skills import expand_skill_directive
from ..zeta.timeline import current_timeline, last_event_time, record_event
from ..zeta.trace import latest_prompt_trace_fields

ASK_WORKFLOW = "ask"
ASK_REQUEST_EVENT = "ask_requested"

ASK_SYSTEM_PROMPT = (
    "Answer concisely. You are responding to a quick question typed at a shell "
    "prompt. The available tools are read, grep, and ls only. Use read for "
    "files, ls for directory contents, file sizes, and recursive size-filtered "
    "listings, and grep to search local text. Do not "
    "propose shell commands just to inspect files or directories; inspect them "
    "through the available tools. If a 'Recent shell activity' block appears "
    "in the user message, it already shows the last few commands. For older "
    "sessions or audit history, the read tool can access ~/.sigil/events.jsonl. "
    "Do not mutate files or execute commands."
)

ZETA_ASK_TOOLS = "read,grep,ls"
ASK_TOOLS = ("read", "grep", "ls")


def parse_tools(tools: str) -> tuple[str, ...]:
    """Parse a comma-separated tool allowlist."""
    return tuple(tool.strip() for tool in tools.split(",") if tool.strip())


def prepend_recent_turns(user_input: str) -> str:
    """Attach shell activity newer than the last model response to a question.

    Older activity is already in the timeline the model sees; only the delta
    since the previous turn is news.
    """
    since = last_event_time()
    sections = []
    context = recent_turns_context(since=since)
    if context:
        sections.append(context)
    failure = active_failure_context(since=since)
    if failure:
        sections.append(failure)
    if not sections:
        return user_input
    sections.append(f"Question:\n{user_input}")
    return "\n\n".join(sections)


RECENT_ASK_TURN_CHARS = 500
FALLBACK_CONTEXT_CHARS = 32_000
FALLBACK_EVENT_TEXT_CHARS = 6_000


def ask(
    question: str,
    *,
    glyph: str = "ask",
    tools: str = ZETA_ASK_TOOLS,
    json_output: bool = False,
) -> int:
    """Run Zeta for a shell ask continuing the session timeline."""
    user_input = question
    selected_model = active_model_selection()
    expanded_input = expand_skill_directive(user_input)
    prompt = prepend_recent_turns(expanded_input)
    request_payload: dict[str, Any] = {
        "type": ASK_REQUEST_EVENT,
        "input": user_input,
        "prompt": prompt,
        "glyph": glyph,
    }
    if selected_model is not None:
        request_payload["model"] = model_selection_event(selected_model)
    append_event(request_payload)
    write_jsonl("last-tools.jsonl", [])
    enabled_tools = parse_tools(tools)
    return run_tool_ask(
        ASK_SYSTEM_PROMPT,
        prompt,
        input_text=user_input,
        json_output=json_output,
        allowed_tools=enabled_tools,
        selected_model=selected_model,
    )


def run_tool_ask(
    system: str,
    prompt: str,
    *,
    input_text: str = "",
    json_output: bool = False,
    max_steps: int | None = None,
    allowed_tools: Iterable[str] = ASK_TOOLS,
    selected_model: ModelSelection | None = None,
) -> int:
    """Run a read-only Zeta ask turn continuing the session timeline."""
    if selected_model is None:
        selected_model = active_model_selection()
    if not model_server_ready(selected_model):
        return 1
    enabled_tools = tuple(allowed_tools)
    renderer = build_turn_renderer(sys.stderr, json_output=json_output)
    context_footer = renderer.context_footer
    recorder = AskEventRecorder(renderer, json_output=json_output)
    user_event: dict[str, Any] = {
        "type": "user_message",
        "content": prompt,
        "runtime": "zeta",
        "workflow": ASK_WORKFLOW,
        "system": system,
        "available_tools": list(enabled_tools),
    }
    if selected_model is not None:
        user_event["model"] = model_selection_event(selected_model)
    prior_timeline = current_timeline()
    record_event(user_event)
    status_enabled = ask_thinking_status_enabled(json_output)
    try:
        result = run_agent_turn(
            prompt,
            prior_timeline,
            AgentConfig(
                system_prompt=system,
                allowed_tools=enabled_tools,
                max_turns=max_steps,
                stop_on_handoff=True,
                model_profile=(
                    selected_model.profile if selected_model is not None else None
                ),
                model_name=selected_model.model if selected_model is not None else None,
                model_url=selected_model.url if selected_model is not None else None,
            ),
            context=load_project_context(),
            event_sink=recorder.record,
            model_status=thinking_status_factory(
                sys.stderr,
                enabled=status_enabled,
                before_start=(
                    context_footer.clear if context_footer is not None else None
                ),
                detail=(
                    context_footer.current_line if context_footer is not None else None
                ),
            ),
            stream_sink=renderer.stream_renderer,
        )
        recorder.replay(result)
        tool_events = list(recorder.tool_events)
        answer = result.final_text
        answer_prompt_traces = result.prompt_traces
        answer_streamed = result.final_text_streamed
        model_telemetry = dict(result.model_telemetry)
        if not answer:
            if context_footer is not None:
                context_footer.clear()
            answer, answer_streamed, fallback_telemetry = (
                run_fallback_answer_with_status(
                    system,
                    prompt,
                    prior_timeline,
                    result.events,
                    selected_model,
                    status_enabled=status_enabled,
                    stream_renderer=renderer.stream_renderer,
                )
            )
            if fallback_telemetry:
                model_telemetry = fallback_telemetry
            answer_prompt_traces = []
            record_event(
                {
                    "type": "assistant_message",
                    "content": answer,
                    "runtime": "zeta",
                    "workflow": ASK_WORKFLOW,
                }
            )
    except RuntimeError as error:
        record_turn_abort(error, workflow=ASK_WORKFLOW)
        raise
    record_answer(
        input_text=input_text,
        prompt=prompt,
        answer=answer,
        tools=tool_events,
        json_output=json_output,
        model=model_selection_event(selected_model) if selected_model else None,
        model_telemetry=model_telemetry,
        prompt_traces=answer_prompt_traces,
        answer_streamed=answer_streamed,
        renderer=renderer,
    )
    return 0


def ask_thinking_status_enabled(json_output: bool) -> bool | None:
    if json_output:
        return False
    return None


class AskEventRecorder(TurnEventRecorder):
    """Persist and render ask workflow events, logging the tool timeline."""

    tag_fields = {"workflow": ASK_WORKFLOW}
    strip_fields = frozenset({"workflow"})

    def __init__(self, renderer: TurnRenderer, *, json_output: bool) -> None:
        super().__init__(renderer, render_output=sys.stderr)
        self.json_output = json_output
        self.tool_events: list[dict[str, Any]] = []

    def handle_tool_call(self, name: str, args: dict[str, Any]) -> None:
        append_jsonl(
            "last-tools.jsonl", {"type": "tool_start", "tool": name, "args": args}
        )
        if not self.json_output:
            self.render_tool_call(name, args)

    def handle_tool_result(self, persisted: dict[str, Any]) -> int | None:
        name = str(persisted.get("name") or "")
        result_payload = persisted.get("result")
        if not isinstance(result_payload, dict):
            result_payload = {}
        if not self.json_output:
            render_tool_result_summary(
                name,
                result_payload,
                output=self.render_output,
                mark_text_separator=self.renderer.trace_state,
            )
            if self.renderer.context_footer is not None:
                self.renderer.context_footer.update_for_tool_result(
                    event_model_telemetry(persisted),
                    result_payload,
                )
        tool_event = {"type": "tool_end", "tool": name, "result": result_payload}
        append_jsonl("last-tools.jsonl", tool_event)
        self.tool_events.append(tool_event)
        return None


def fallback_turn_context(
    prompt: str,
    prior_events: list[dict[str, Any]],
    current_events: list[dict[str, Any]],
) -> str:
    """Return a model-readable evidence digest for fallback answers."""
    sections = [f"Current question:\n{prompt}"]
    history = fallback_history_lines(prior_events)
    if history:
        sections.append("Prior conversation:\n" + "\n".join(history))
    observations = fallback_observation_blocks(current_events)
    if observations:
        sections.append("Current turn observations:\n" + "\n\n".join(observations))
    return clamp_text("\n\n".join(sections), FALLBACK_CONTEXT_CHARS)


ROLE_BY_TIMELINE_TYPE = {"user_message": "user", "assistant_message": "assistant"}


def fallback_history_lines(prior_events: list[dict[str, Any]]) -> list[str]:
    lines = []
    for event in prior_events:
        role = str(event.get("role") or "") or ROLE_BY_TIMELINE_TYPE.get(
            str(event.get("type") or ""), ""
        )
        if role not in {"user", "assistant"}:
            continue
        content = str(event.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {clamp_text(content, RECENT_ASK_TURN_CHARS)}")
    return lines


def fallback_observation_blocks(turn_events: list[dict[str, Any]]) -> list[str]:
    blocks = []
    for event in turn_events:
        event_type = str(event.get("type") or "")
        if event_type == "assistant_message":
            content = str(event.get("content") or "").strip()
            if content:
                blocks.append(
                    "Assistant note:\n" + clamp_text(content, RECENT_ASK_TURN_CHARS)
                )
            continue
        if event_type == "tool_result":
            block = fallback_tool_result_block(event)
            if block:
                blocks.append(block)
    return blocks


def fallback_tool_result_block(event: dict[str, Any]) -> str:
    result = event.get("result")
    if not isinstance(result, dict):
        return ""
    label = fallback_tool_result_label(str(event.get("name") or "tool"), result)
    text = fallback_tool_result_text(result)
    if text:
        return f"{label}:\n{clamp_text(text, FALLBACK_EVENT_TEXT_CHARS)}"
    metadata = result.get("metadata")
    if isinstance(metadata, dict) and metadata:
        return f"{label} metadata:\n{json.dumps(metadata, ensure_ascii=False)}"
    error = result.get("error")
    if isinstance(error, dict) and error:
        return f"{label} error:\n{json.dumps(error, ensure_ascii=False)}"
    if result.get("ok") is True:
        return f"{label}: ok"
    return ""


def fallback_tool_result_label(name: str, result: dict[str, Any]) -> str:
    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        return f"Tool result ({name})"
    details = [name]
    path = metadata.get("path")
    if isinstance(path, str) and path:
        details.append(path)
    pattern = metadata.get("pattern")
    if isinstance(pattern, str) and pattern:
        details.append(f"pattern={pattern}")
    return "Tool result (" + " ".join(details) + ")"


def fallback_tool_result_text(result: dict[str, Any]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(part.get("text") or "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    ).strip()


def clamp_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n... truncated ..."


def fallback_answer(
    system: str,
    prompt: str,
    prior_events: list[dict[str, Any]],
    current_events: list[dict[str, Any]],
    selected_model: ModelSelection | None = None,
    stream_sink: ChatCompletionStreamSink | None = None,
    telemetry_sink: Callable[[dict[str, Any]], None] | None = None,
) -> str:
    """Answer from a compact current-turn evidence digest when stepping stalls."""
    fallback_prompt = "\n\n".join(
        [
            "Answer the user's question using only the conversation and local "
            "tool evidence below.",
            "Do not request tools. If the evidence is insufficient, say which "
            "fact is missing in terms of the user's question; do not give a "
            "meta-answer about transcript completeness.",
            fallback_turn_context(prompt, prior_events, current_events),
        ]
    )
    if selected_model is None:
        answer = chat_text(
            system,
            fallback_prompt,
            stream_sink=stream_sink,
            telemetry_sink=telemetry_sink,
        ).strip()
    else:
        answer = chat_text(
            system,
            fallback_prompt,
            selected_model=selected_model.model,
            selected_url=selected_model.url,
            stream_sink=stream_sink,
            telemetry_sink=telemetry_sink,
        ).strip()
    if answer:
        return answer
    return "I could not answer from the available local context."


def run_fallback_answer_with_status(
    system: str,
    prompt: str,
    prior_events: list[dict[str, Any]],
    current_events: list[dict[str, Any]],
    selected_model: ModelSelection | None,
    *,
    status_enabled: bool | None,
    stream_renderer: StreamRenderer | None,
) -> tuple[str, bool, dict[str, Any]]:
    fallback_sink = (
        StreamDeltaTracker(stream_renderer) if stream_renderer is not None else None
    )
    model_telemetry: dict[str, Any] = {}
    status = ThinkingStatus(sys.stderr, enabled=status_enabled)
    status_open = False

    def close_status(
        exc_type: type[BaseException] | None = None,
        exc: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        nonlocal status_open
        if not status_open:
            return
        status_open = False
        status.__exit__(exc_type, exc, traceback)

    if fallback_sink is not None:
        fallback_sink.before_first_delta = lambda: close_status(None, None, None)
    status.__enter__()
    status_open = True
    try:
        answer = fallback_answer(
            system,
            prompt,
            prior_events,
            current_events,
            selected_model,
            stream_sink=fallback_sink,
            telemetry_sink=model_telemetry.update,
        )
    except BaseException as exc:
        close_status(type(exc), exc, exc.__traceback__)
        raise
    close_status()
    return (
        answer,
        bool(fallback_sink and fallback_sink.streamed_content),
        model_telemetry,
    )


def record_answer(
    *,
    input_text: str,
    prompt: str,
    answer: str,
    tools: list[dict[str, Any]],
    json_output: bool,
    model: dict[str, str] | None,
    model_telemetry: dict[str, Any] | None = None,
    prompt_traces: list[Any] | tuple[Any, ...] = (),
    answer_streamed: bool = False,
    renderer: TurnRenderer | None = None,
) -> None:
    telemetry_fields = model_telemetry_fields(model_telemetry)
    trace_fields = latest_prompt_trace_fields(prompt_traces)
    answer_event: dict[str, Any] = {
        "type": "answer",
        "input": input_text,
        "prompt": prompt,
        "answer": answer,
        "runtime": "zeta",
        "workflow": ASK_WORKFLOW,
        **telemetry_fields,
        **trace_fields,
    }
    if model is not None:
        answer_event["model"] = model
    append_event(answer_event)
    if json_output:
        print(
            json.dumps(
                {
                    "question": input_text,
                    "prompt": prompt,
                    "answer": answer,
                    "runtime": "zeta",
                    "tools": tools,
                    "malformed_events": 0,
                    **telemetry_fields,
                    **trace_fields,
                    **({"model": model} if model is not None else {}),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    context_footer = renderer.context_footer if renderer is not None else None
    if answer_streamed:
        if renderer is not None:
            render_final_text(answer, streamed=True, renderer=renderer)
        if context_footer is not None:
            context_footer.finalize(model_telemetry)
        return
    if context_footer is not None:
        context_footer.clear()
    if renderer is not None:
        render_final_text(answer, streamed=False, renderer=renderer)
    else:
        print()
        print(answer)
        print()
    if context_footer is not None:
        context_footer.finalize(model_telemetry)


class StreamDeltaTracker:
    """Track whether a nested stream sink emitted visible text."""

    def __init__(
        self,
        stream_sink: StreamRenderer,
        before_first_delta: Callable[[], None] | None = None,
    ) -> None:
        self.stream_sink = stream_sink
        self.before_first_delta = before_first_delta
        self.streamed_content = False

    def content_delta(self, text: str) -> None:
        if not text:
            return
        if not self.streamed_content and self.before_first_delta is not None:
            self.before_first_delta()
        self.streamed_content = True
        self.stream_sink.content_delta(text)
