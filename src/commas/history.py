"""Turn/effect history derived from durable events."""

from __future__ import annotations

import os
import re
import time
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, cast

from zeta.records.events import Event
from zeta.records.stores.event_store import Filter
from zeta.records.stores.sqlite import SqliteEventStore
from zeta.run.events import (
    TURN_RECORD_SCHEMA,
    turn_event_type,
)

SINCE_PATTERN = re.compile(r"(\d+)([dhm])")
SINCE_SCALES = {"d": 86400, "h": 3600, "m": 60}
EFFECT_RECORD_TYPE = "zeta.effect"
EFFECT_RECORD_SCHEMA = "zeta.effect"


class UnknownTurnError(LookupError):
    """A turn id token matched no record or prefix."""

    def __init__(self, token: str) -> None:
        super().__init__(token)
        self.token = token


class AmbiguousTurnError(LookupError):
    """A turn id prefix matched more than one record."""

    def __init__(self, token: str, candidates: list[str]) -> None:
        super().__init__(token)
        self.token = token
        self.candidates = candidates


def parse_since(value: str) -> float:
    """Parse a YYYY-MM-DD date or an age like 2d/6h/30m into an epoch bound."""
    relative = SINCE_PATTERN.fullmatch(value.strip())
    if relative is not None:
        return time.time() - int(relative.group(1)) * SINCE_SCALES[relative.group(2)]
    return time.mktime(time.strptime(value.strip(), "%Y-%m-%d"))


def touched_path_variants(path: str) -> tuple[str, ...]:
    """Return the path as given plus its absolute form, deduplicated."""
    variants = [path]
    absolute = os.path.abspath(path)
    if absolute not in variants:
        variants.append(absolute)
    return tuple(variants)


def resolve_turn_id(history: HistoryView, token: str) -> str:
    """Resolve a full turn id or unique prefix, or raise with candidates."""
    if history.turn(token) is not None:
        return token
    matches = history.turn_ids_with_prefix(token)
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise AmbiguousTurnError(token, matches)
    raise UnknownTurnError(token)


class HistoryView:
    """Derived turn/effect history over durable events."""

    def __init__(self, events: list[Event]) -> None:
        self._turns_by_id = project_turn_records_by_id(events)
        self._effects_by_id = project_effect_records_by_id(events)

    @classmethod
    def from_store(cls, path: str | Path) -> HistoryView:
        return cls(SqliteEventStore(path).list_events(Filter()))

    def query_turns(
        self,
        *,
        session: str | None = None,
        workflow: str | None = None,
        since: float | None = None,
        failed: bool = False,
        touched: tuple[str, ...] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return turn records newest first, narrowed by the given filters."""
        touched_turns = self._touched_turn_ids(touched)
        turns = [
            turn
            for turn in self._turns_by_id.values()
            if turn_matches_filters(
                turn,
                session=session,
                workflow=workflow,
                since=since,
                failed=failed,
                touched_turns=touched_turns,
            )
        ]
        turns.sort(key=turn_sort_key, reverse=True)
        return turns[:limit] if limit is not None else turns

    def _touched_turn_ids(self, touched: tuple[str, ...] | None) -> set[str] | None:
        if touched is None:
            return None
        return {
            str(effect.get("turn_id") or "")
            for effect in self._effects_by_id.values()
            if effect.get("path") in touched
        }

    def turn_ids_with_prefix(self, prefix: str, limit: int = 16) -> list[str]:
        """Return turn ids starting with a prefix, sorted, bounded."""
        matches = [
            turn_id for turn_id in self._turns_by_id if turn_id.startswith(prefix)
        ]
        return sorted(matches)[:limit]

    def pending_staged_command(self, session: str) -> dict[str, Any] | None:
        """Return the newest staged command effect awaiting resolution."""
        effects = list(self._effects_by_id.values())
        resolved_calls = resolved_tool_call_ids(effects)
        candidates = [
            effect
            for effect in effects
            if is_pending_staged_command(
                effect,
                session=session,
                resolved_calls=resolved_calls,
            )
        ]
        candidates.sort(key=effect_sort_key, reverse=True)
        return candidates[0] if candidates else None

    def cost_since(self, session: str, since: float) -> dict[str, int]:
        """Sum the session's turn costs recorded at or after a time."""
        totals = {"input_tokens": 0, "output_tokens": 0, "model_calls": 0, "turns": 0}
        for turn in self.query_turns(session=session, since=since):
            totals["turns"] += 1
            cost = turn.get("cost")
            if not isinstance(cost, dict):
                continue
            totals["input_tokens"] += int(cost.get("input_tokens") or 0)
            totals["output_tokens"] += int(cost.get("output_tokens") or 0)
            totals["model_calls"] += int(cost.get("model_calls") or 0)
        return totals

    def turn(self, turn_id: str) -> dict[str, Any] | None:
        return self._turns_by_id.get(turn_id)

    def effect(self, effect_id: str) -> dict[str, Any] | None:
        return self._effects_by_id.get(effect_id)

    def turns(self, limit: int | None = None) -> list[dict[str, Any]]:
        return self.query_turns(limit=limit)

    def effects(self) -> list[dict[str, Any]]:
        effects = list(self._effects_by_id.values())
        effects.sort(key=effect_sort_key)
        return effects

    def effects_for_turn(self, turn_id: str) -> list[dict[str, Any]]:
        effects = [
            effect
            for effect in self._effects_by_id.values()
            if effect.get("turn_id") == turn_id
        ]
        effects.sort(key=effect_sort_key)
        return effects

    def effects_touching(self, path: str) -> list[dict[str, Any]]:
        effects = [
            effect
            for effect in self._effects_by_id.values()
            if effect.get("path") == path
        ]
        effects.sort(key=effect_sort_key)
        return effects


def project_turn_records_by_id(events: list[Event]) -> dict[str, dict[str, Any]]:
    turns: dict[str, dict[str, Any]] = {}
    for event in events:
        if not event.event_type.startswith("zeta.turn."):
            continue
        record = project_one_turn_record(event)
        turn_id = str(record.get("turn_id") or "")
        if turn_id:
            turns[turn_id] = record
    return turns


def project_effect_records_by_id(events: list[Event]) -> dict[str, dict[str, Any]]:
    effects: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.event_type != "zeta.tool_call.completed":
            continue
        raw_effects = event.payload.get("effects")
        if not isinstance(raw_effects, list):
            continue
        for effect in raw_effects:
            if not is_effect_record(effect):
                continue
            record = project_one_effect_record(
                effect,
                timestamp=event.timestamp_ms / 1_000,
                session_id=event.session_id,
                cwd=event.payload.get("cwd"),
            )
            effect_id = str(record.get("effect_id") or "")
            if effect_id:
                effects[effect_id] = record
    return effects


def turn_record(
    turn_id: str,
    *,
    workflow: str,
    objective: str,
    contract: Mapping[str, Any],
    outcome: str,
    agent: Mapping[str, str] | None = None,
    cost: Mapping[str, int] | None = None,
    prompt_object_ids: Iterable[str] = (),
    effect_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Return one turn history record; the event envelope adds time/cwd/session."""
    record: dict[str, Any] = {
        "type": turn_event_type(outcome),
        "schema": TURN_RECORD_SCHEMA,
        "turn_id": turn_id,
        "workflow": workflow,
        "objective": objective,
        "contract": dict(contract),
        "outcome": outcome,
        "prompt_object_ids": list(prompt_object_ids),
        "effect_ids": list(effect_ids),
    }
    if agent is not None:
        record["agent"] = dict(agent)
    if cost is not None:
        record["cost"] = dict(cost)
    if outcome == "aborted":
        record["reason"] = "aborted"
    return record


def effect_record(
    effect_id: str,
    *,
    turn_id: str,
    kind: str,
    staged: bool,
    path: str | None = None,
    before_hash: str | None = None,
    after_hash: str | None = None,
    command: str | None = None,
    exit_status: int | None = None,
    duration_ms: int | None = None,
    tool_call_id: str | None = None,
    resolved_outcome: str | None = None,
) -> dict[str, Any]:
    """Return one turn effect record; unset optional facts are omitted."""
    record: dict[str, Any] = {
        "type": EFFECT_RECORD_TYPE,
        "schema": EFFECT_RECORD_SCHEMA,
        "effect_id": effect_id,
        "turn_id": turn_id,
        "kind": kind,
        "staged": staged,
    }
    optionals: dict[str, Any] = {
        "path": path,
        "before_hash": before_hash,
        "after_hash": after_hash,
        "command": command,
        "exit_status": exit_status,
        "duration_ms": duration_ms,
        "tool_call_id": tool_call_id,
        "resolved_outcome": resolved_outcome,
    }
    record.update({key: value for key, value in optionals.items() if value is not None})
    return record


def is_turn_record(value: object) -> bool:
    return has_schema(value, TURN_RECORD_SCHEMA)


def is_effect_record(value: object) -> bool:
    return has_schema(value, EFFECT_RECORD_SCHEMA)


def has_schema(value: object, schema: str) -> bool:
    if not isinstance(value, Mapping):
        return False
    record = cast(Mapping[str, Any], value)
    return record.get("schema") == schema


def publish_turn_record(
    record: dict[str, Any],
    *,
    path: str | Path,
    session_id: str,
    cwd: str | None = None,
) -> Event:
    """Append one durable turn record to a Zeta event store."""
    event_type = turn_event_type(str(record.get("outcome") or ""))
    payload = dict(record)
    if payload.get("outcome") == "aborted":
        payload.setdefault("reason", "aborted")
    return (
        SqliteEventStore(path)
        .append(
            event_from_record(
                {
                    "cwd": cwd or os.getcwd(),
                    **payload,
                    "type": event_type,
                    "session": session_id,
                }
            )
        )
        .event
    )


def publish_effect_record(
    record: dict[str, Any],
    *,
    path: str | Path,
    session_id: str,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Append one durable tool-effect event and return its history record."""
    appended = (
        SqliteEventStore(path)
        .append(
            event_from_effect_record(
                {
                    "cwd": cwd or os.getcwd(),
                    "session": session_id,
                    **record,
                }
            )
        )
        .event
    )
    return project_one_effect_record(
        record,
        timestamp=appended.timestamp_ms / 1_000,
        session_id=appended.session_id or session_id,
        cwd=appended.payload.get("cwd"),
    )


def import_history_records(
    history: HistoryView,
    records: list[dict[str, Any]],
    *,
    path: str | Path,
) -> int:
    """Import new turn/effect records into a Zeta event store."""
    imported = 0
    imported_turn_ids: set[str] = set()
    imported_effect_ids: set[str] = set()
    store = SqliteEventStore(path)
    for record in records:
        if not isinstance(record, dict):
            continue
        if is_turn_record(record):
            record_id = str(record.get("turn_id") or "")
            if record_id in imported_turn_ids or history.turn(record_id) is not None:
                continue
            imported_turn_ids.add(record_id)
            store.append(event_from_record(record))
            imported += 1
            continue
        if is_effect_record(record):
            record_id = str(record.get("effect_id") or "")
            if (
                record_id in imported_effect_ids
                or history.effect(record_id) is not None
            ):
                continue
            imported_effect_ids.add(record_id)
            store.append(event_from_effect_record(record))
            imported += 1
    return imported


def query_history(
    history: HistoryView,
    *,
    session: str | None = None,
    workflow: str | None = None,
    since: str | None = None,
    failed: bool = False,
    touched: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Parse user-facing filters and return matching turn records."""
    return history.query_turns(
        session=session,
        workflow=workflow,
        since=parse_since(since) if since else None,
        failed=failed,
        touched=touched_path_variants(touched) if touched else None,
        limit=limit,
    )


def turn_matches_filters(
    turn: dict[str, Any],
    *,
    session: str | None,
    workflow: str | None,
    since: float | None,
    failed: bool,
    touched_turns: set[str] | None,
) -> bool:
    turn_id = str(turn.get("turn_id") or "")
    checks = (
        session is None or turn.get("session") == session,
        workflow is None or turn.get("workflow") == workflow,
        since is None or float(turn.get("time") or 0.0) >= since,
        not failed or turn.get("outcome") in {"failed", "aborted"},
        touched_turns is None or turn_id in touched_turns,
    )
    return all(checks)


def turn_sort_key(turn: dict[str, Any]) -> tuple[float, str]:
    return (float(turn.get("time") or 0.0), str(turn.get("turn_id") or ""))


def resolved_tool_call_ids(effects: list[dict[str, Any]]) -> set[str]:
    return {
        str(effect.get("tool_call_id") or "")
        for effect in effects
        if effect.get("resolved_outcome") is not None and effect.get("tool_call_id")
    }


def is_pending_staged_command(
    effect: dict[str, Any],
    *,
    session: str,
    resolved_calls: set[str],
) -> bool:
    tool_call_id = str(effect.get("tool_call_id") or "")
    return (
        effect.get("session") == session
        and effect.get("staged") is True
        and effect.get("kind") == "command"
        and bool(tool_call_id)
        and tool_call_id not in resolved_calls
    )


def effect_sort_key(effect: dict[str, Any]) -> tuple[float, str]:
    return (float(effect.get("time") or 0.0), str(effect.get("effect_id") or ""))


def project_one_turn_record(event: Event) -> dict[str, Any]:
    record = dict(event.payload)
    record.update(
        {
            "id": event.id,
            "type": event.event_type,
            "time": event.timestamp_ms / 1_000,
        }
    )
    if event.session_id is not None:
        record["session"] = event.session_id
    if event.caused_by is not None:
        record["caused_by"] = event.caused_by
    return record


def project_one_effect_record(
    record: dict[str, Any],
    *,
    timestamp: float,
    session_id: str | None,
    cwd: Any = None,
) -> dict[str, Any]:
    payload = {"cwd": cwd if isinstance(cwd, str) else os.getcwd(), **record}
    payload["time"] = timestamp
    if session_id is not None:
        payload["session"] = session_id
    return payload


def event_from_effect_record(record: dict[str, Any]) -> Event:
    return Event(
        id=str(record.get("id") or record["effect_id"]),
        event_type="zeta.tool_call.completed",
        source="zeta",
        payload={
            "cwd": record.get("cwd"),
            "turn_id": record.get("turn_id"),
            "effects": [record],
        },
        idempotency_key=None,
        caused_by=(
            str(record["caused_by"])
            if isinstance(record.get("caused_by"), str)
            else None
        ),
        session_id=(
            str(record["session"]) if isinstance(record.get("session"), str) else None
        ),
        run_id=None,
        turn_id=(
            str(record["turn_id"]) if isinstance(record.get("turn_id"), str) else None
        ),
        timestamp_ms=(
            int(float(record["time"]) * 1_000)
            if isinstance(record.get("time"), int | float)
            and not isinstance(record.get("time"), bool)
            else 0
        ),
    )


def event_from_record(record: dict[str, Any]) -> Event:
    payload = {
        key: value
        for key, value in record.items()
        if key not in {"id", "type", "time", "session", "source", "caused_by"}
    }
    return Event(
        id=str(record.get("id") or f"evt_{uuid.uuid4().hex}"),
        event_type=str(record["type"]),
        source=str(record.get("source") or "zeta"),
        payload=payload,
        idempotency_key=None,
        caused_by=(
            str(record["caused_by"])
            if isinstance(record.get("caused_by"), str)
            else None
        ),
        session_id=(
            str(record["session"]) if isinstance(record.get("session"), str) else None
        ),
        run_id=(
            str(record["run_id"]) if isinstance(record.get("run_id"), str) else None
        ),
        turn_id=(
            str(record["turn_id"]) if isinstance(record.get("turn_id"), str) else None
        ),
        timestamp_ms=(
            int(float(record["time"]) * 1_000)
            if isinstance(record.get("time"), int | float)
            and not isinstance(record.get("time"), bool)
            else 0
        ),
    )
