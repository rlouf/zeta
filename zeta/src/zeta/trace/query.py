"""Trace object and runtime-history query helpers."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import click

from zeta.events import Event
from zeta.journal.store import EventReader, Filter
from zeta.substrate import Object, ObjectId
from zeta.substrate.store import (
    AmbiguousIdError,
    Store,
    UnknownIdError,
    resolve_object_id,
)
from zeta.trace.summarize import (
    estimated_prompt_tokens,
    format_tool_error,
    summarize,
    truncate,
)

DEFAULT_QUERY_LOG_LIMIT = 20
MAX_QUERY_LOG_LIMIT = 50
MAX_QUERY_LOG_EVENTS = 5_000
MAX_QUERY_LOG_OUTPUT_CHARS = 12_000
QueryLogReader = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class RunLogRecord:
    run_id: str
    events: tuple[Event, ...]

    @property
    def started_at_ms(self) -> int:
        return min(event.timestamp_ms for event in self.events)


def bind_query_log_reader(
    event_reader: EventReader,
    *,
    session_id: str,
    current_run_id: str,
) -> QueryLogReader:
    """Bind history access to one authorized runtime session and active run."""

    def read(params: dict[str, Any]) -> dict[str, Any]:
        return query_run_log(
            params,
            event_reader=event_reader,
            session_id=session_id,
            current_run_id=current_run_id,
        )

    return read


def query_run_log(
    params: dict[str, Any],
    *,
    event_reader: EventReader,
    session_id: str,
    current_run_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Query prior runs without allowing the model to choose another session."""
    records = session_run_records(
        event_reader,
        session_id=session_id,
        current_run_id=current_run_id,
    )
    run_token = str(params.get("run_id") or "").strip()
    if run_token:
        return expand_run_log(records, run_token)
    try:
        since_ms = parse_query_log_since(
            str(params.get("since") or ""),
            now=now,
        )
    except ValueError:
        return query_log_error(
            "invalid-since",
            "since must be YYYY-MM-DD or an age like 2d, 6h, or 30m",
        )
    if since_ms is not None:
        records = [record for record in records if record.started_at_ms >= since_ms]
    if params.get("failed") is True:
        records = [
            record for record in records if run_outcome(record) in FAILED_OUTCOMES
        ]
    limit = min(
        max(int(params.get("limit") or DEFAULT_QUERY_LOG_LIMIT), 1), MAX_QUERY_LOG_LIMIT
    )
    selected = records[:limit]
    if not selected:
        return query_log_success(
            "no prior runs recorded",
            {
                "runs": 0,
                "run_ids": [],
                "session_id": session_id,
                "limit": limit,
            },
        )
    return query_log_success(
        "\n".join(run_log_summary(record) for record in selected),
        {
            "runs": len(selected),
            "run_ids": [record.run_id for record in selected],
            "session_id": session_id,
            "limit": limit,
        },
    )


def session_run_records(
    event_reader: EventReader,
    *,
    session_id: str,
    current_run_id: str,
) -> list[RunLogRecord]:
    grouped: dict[str, list[Event]] = {}
    newest_events = event_reader.list_events(
        Filter(
            session_id=session_id,
            limit=MAX_QUERY_LOG_EVENTS,
            newest_first=True,
        )
    )
    for event in reversed(newest_events):
        if event.run_id is None or event.run_id == current_run_id:
            continue
        grouped.setdefault(event.run_id, []).append(event)
    records = [
        RunLogRecord(run_id, tuple(events))
        for run_id, events in grouped.items()
        if is_model_run(events)
    ]
    return sorted(records, key=lambda record: record.started_at_ms, reverse=True)


def is_model_run(events: list[Event]) -> bool:
    return any(
        event.event_type in {"session.turn.requested", "zeta.user_message"}
        for event in events
    )


FAILED_OUTCOMES = {"aborted", "failed"}


def run_outcome(record: RunLogRecord) -> str:
    terminal = terminal_run_result(record)
    if terminal is not None:
        outcome = terminal.get("outcome")
        if isinstance(outcome, str) and outcome:
            return outcome
    event_types = {event.event_type for event in record.events}
    if "zeta.turn.failed" in event_types:
        return "aborted"
    if event_types & {
        "runtime.queue_item.dead_lettered",
        "runtime.queue_item.failed",
        "runtime.queue_item.unhandled",
    }:
        return "failed"
    if any(
        event.event_type == "zeta.model_call.completed"
        and event.payload.get("_timeline_type") == "model"
        for event in record.events
    ):
        return "completed"
    return "running"


def terminal_run_result(record: RunLogRecord) -> dict[str, Any] | None:
    terminal_types = {
        "runtime.queue_item.completed",
        "runtime.queue_item.cancelled",
        "runtime.queue_item.dead_lettered",
        "runtime.queue_item.failed",
    }
    fallback: dict[str, Any] | None = None
    for event in reversed(record.events):
        if event.event_type not in terminal_types:
            continue
        result = event.payload.get("result")
        if not isinstance(result, dict):
            continue
        target = event.payload.get("target_agent")
        if target == "zeta.session.turn":
            return result
        if fallback is None:
            fallback = result
    return fallback


def run_objective(record: RunLogRecord) -> str:
    for event in record.events:
        if event.event_type == "session.turn.requested":
            objective = event.payload.get("objective")
            if isinstance(objective, str) and objective:
                return objective
        if event.event_type == "zeta.user_message":
            content = event.payload.get("content")
            if isinstance(content, str) and content:
                return content
    return ""


def run_final_answer(record: RunLogRecord) -> str:
    terminal = terminal_run_result(record)
    if terminal is not None:
        answer = terminal.get("final_answer")
        if isinstance(answer, str) and answer:
            return answer
    for event in reversed(record.events):
        if (
            event.event_type == "zeta.model_call.completed"
            and event.payload.get("_timeline_type") == "model"
        ):
            content = event.payload.get("content")
            if isinstance(content, str) and content:
                return content
    return ""


def run_tool_lines(record: RunLogRecord) -> list[str]:
    calls: dict[str, dict[str, Any]] = {}
    lines: list[str] = []
    for event in record.events:
        call_id = str(event.payload.get("tool_call_id") or "")
        if event.event_type == "zeta.tool_call.started":
            calls[call_id] = dict(event.payload)
            continue
        if event.event_type not in {
            "zeta.tool_call.completed",
            "zeta.tool_call.failed",
        }:
            continue
        call = calls.get(call_id, {})
        name = str(event.payload.get("name") or call.get("name") or "tool")
        result = event.payload.get("result")
        ok = result.get("ok") if isinstance(result, dict) else None
        failed = ok is False or event.event_type == "zeta.tool_call.failed"
        status = "failed" if failed else "ok" if ok is True else "unknown"
        input_summary = truncate(summarize(name, call.get("input")), 180)
        failure_summary = ""
        if failed and isinstance(result, dict):
            error = result.get("error")
            if isinstance(error, dict):
                failure_summary = truncate(format_tool_error(error), 180)
        detail = " · ".join(part for part in (input_summary, failure_summary) if part)
        line = f"{name}: {status}" + (f" · {detail}" if detail else "")
        lines.append(line)
    return lines


def run_prompt_ids(record: RunLogRecord) -> list[str]:
    prompt_ids: list[str] = []
    for event in record.events:
        prompt_id = event.payload.get("prompt_object_id")
        if isinstance(prompt_id, str) and prompt_id not in prompt_ids:
            prompt_ids.append(prompt_id)
    terminal = terminal_run_result(record)
    trace = terminal.get("trace") if terminal is not None else None
    stored = trace.get("prompt_ids") if isinstance(trace, dict) else None
    if isinstance(stored, list):
        prompt_ids.extend(
            prompt_id
            for prompt_id in stored
            if isinstance(prompt_id, str) and prompt_id not in prompt_ids
        )
    return prompt_ids


def run_log_summary(record: RunLogRecord) -> str:
    timestamp = datetime.fromtimestamp(record.started_at_ms / 1000, tz=UTC).isoformat()
    objective = one_line(run_objective(record), limit=120)
    return "  ".join(
        part
        for part in (record.run_id, run_outcome(record), timestamp, objective)
        if part
    )


def expand_run_log(records: list[RunLogRecord], token: str) -> dict[str, Any]:
    exact = next((record for record in records if record.run_id == token), None)
    if exact is not None:
        return expanded_run_log_result(exact)
    matches = [record for record in records if record.run_id.startswith(token)]
    if not matches:
        return query_log_error("unknown-run-id", f"no run matches '{token}'")
    if len(matches) > 1:
        candidates = ", ".join(record.run_id for record in matches)
        return query_log_error(
            "ambiguous-run-id",
            f"'{token}' matches: {candidates}",
        )
    return expanded_run_log_result(matches[0])


def expanded_run_log_result(record: RunLogRecord) -> dict[str, Any]:
    lines = [
        f"run      {record.run_id}",
        f"started  {datetime.fromtimestamp(record.started_at_ms / 1000, tz=UTC).isoformat()}",
        f"outcome  {run_outcome(record)}",
        f"objective {run_objective(record)}",
    ]
    answer = run_final_answer(record)
    if answer:
        lines.append(f"answer   {answer}")
    tools = run_tool_lines(record)
    if tools:
        lines.extend(["tools", *(f"  {line}" for line in tools)])
    prompt_ids = run_prompt_ids(record)
    if prompt_ids:
        lines.extend(["prompts", *(f"  {prompt_id}" for prompt_id in prompt_ids)])
    return query_log_success(
        "\n".join(lines),
        {"run_id": record.run_id},
    )


def parse_query_log_since(value: str, *, now: datetime | None = None) -> int | None:
    if not value:
        return None
    reference = now or datetime.now(tz=UTC)
    age = re.fullmatch(r"(\d+)([dhm])", value)
    if age is not None:
        try:
            amount = int(age.group(1))
            delta = {
                "d": timedelta(days=amount),
                "h": timedelta(hours=amount),
                "m": timedelta(minutes=amount),
            }[age.group(2)]
        except OverflowError as exc:
            raise ValueError(value) from exc
        return int((reference - delta).timestamp() * 1000)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError(value) from exc
    return int(parsed.timestamp() * 1000)


def query_log_success(text: str, metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "content": [{"type": "text", "text": bounded_query_log_text(text)}],
        "metadata": metadata,
    }


def query_log_error(code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {"code": code, "message": one_line(message, limit=1_024)},
    }


def bounded_query_log_text(text: str) -> str:
    if len(text) <= MAX_QUERY_LOG_OUTPUT_CHARS:
        return text
    return text[: MAX_QUERY_LOG_OUTPUT_CHARS - 1].rstrip() + "…"


def one_line(value: str, *, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def resolve_cli_object_id(token: str, *, store: Store) -> ObjectId:
    """Resolve a CLI id token, mapping resolver errors onto CLI errors."""
    try:
        return resolve_object_id(store, token)
    except AmbiguousIdError as error:
        candidates = "\n  ".join(error.candidates)
        raise click.ClickException(
            f"ambiguous trace id '{token}' matches:\n  {candidates}"
        ) from error
    except UnknownIdError as error:
        raise click.ClickException(f"trace object not found: {token}") from error


def resolve_cli_prompt(store: Store, token: str) -> tuple[ObjectId, Object]:
    """Resolve a CLI id token to a prompt object, or fail with its kind."""
    object_id = resolve_cli_object_id(token, store=store)
    obj = store.get_object(object_id)
    if obj is None or obj.kind != "prompt":
        kind = obj.kind if obj is not None else "missing"
        raise click.ClickException(f"not a prompt: {token} ({kind})")
    return object_id, obj


def get_trace_object(
    object_id: ObjectId,
    *,
    store: Store,
) -> dict[str, Any] | None:
    obj = store.get_object(object_id)
    if obj is None:
        return None
    return {
        "id": object_id,
        "object": {
            "kind": obj.kind,
            "schema": obj.schema,
            "data": obj.data,
            "links": list(obj.links),
        },
        "derivations": [
            {
                "producer": derivation.producer,
                "output_id": derivation.output_id,
                "input_ids": list(derivation.input_ids),
                "params": derivation.params,
            }
            for derivation in store.derivations_for_output(object_id)
        ],
    }


def list_trace_closure(object_id: ObjectId, *, store: Store) -> list[dict[str, Any]]:
    closure = store.graph_closure([object_id])
    return [
        {"id": closure_id, "kind": obj.kind, "schema": obj.schema}
        for closure_id, obj in closure.items()
        if closure_id != object_id
    ]


def list_trace_refs(*, store: Store) -> dict[str, ObjectId]:
    return {ref.name: ref.object_id for ref in store.refs()}


def list_trace_prompts(*, store: Store) -> list[dict[str, Any]]:
    prompts = []
    for prompt_id, _ in store.objects(kind="prompt"):
        obj = store.get_object(prompt_id)
        if obj is None:
            continue
        prompts.append(
            {
                "id": prompt_id,
                "components": len(obj.links),
                "estimated_tokens": estimated_prompt_tokens(
                    obj.links, store.get_object
                ),
            }
        )
    return prompts
