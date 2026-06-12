"""Portable ledger bundles.

A bundle carries turn and effect records verbatim plus, per session,
the trace-graph closure of each exported turn: objects, derivations
with their original timestamps, and the `turn/<id>` refs. Importing
appends the records to the event log (so `log reindex` replays them
like native ones) and materializes per-session trace stores, so every
ledger and explorer query works on the importing machine.
"""

from __future__ import annotations

from typing import Any

from .ledger import EVENT_LOG_NAME, LedgerIndex, default_ledger_index
from .protocols import is_effect_record, is_turn_record
from .state import append_jsonl_line, rotate_oversized_log, state_dir
from .zeta.trace import (
    Derivation,
    Object,
    SqliteStore,
    UnknownSessionError,
    default_store,
    session_sqlite_path,
)

BUNDLE_VERSION = 1


def export_bundle(
    *,
    since: float | None = None,
    session: str | None = None,
) -> dict[str, Any]:
    """Collect matching turns, their effects, and their trace closures."""
    index = default_ledger_index()
    records: list[dict[str, Any]] = []
    turn_ids_by_session: dict[str, list[str]] = {}
    for turn in index.query_turns(session=session, since=since):
        turn_id = str(turn.get("turn_id") or "")
        records.append(turn)
        records.extend(index.effects_for_turn(turn_id))
        session_id = str(turn.get("session") or "")
        turn_ids_by_session.setdefault(session_id, []).append(turn_id)
    sessions: dict[str, dict[str, Any]] = {}
    for session_id, turn_ids in sorted(turn_ids_by_session.items()):
        graph = exported_session_graph(session_id, turn_ids)
        if graph is not None:
            sessions[session_id] = graph
    return {"sigil_bundle": BUNDLE_VERSION, "records": records, "sessions": sessions}


def exported_session_graph(
    session_id: str,
    turn_ids: list[str],
) -> dict[str, Any] | None:
    """Export one session's closure for the given turns, or None.

    A session whose trace store is gone (cleared, or never recorded)
    still exports its ledger records; only the graph section is absent.
    """
    try:
        store = default_store(session_id=session_id)
    except UnknownSessionError:
        return None
    try:
        refs: dict[str, str] = {}
        for turn_id in turn_ids:
            target = store.get_ref(f"turn/{turn_id}")
            if target is not None:
                refs[f"turn/{turn_id}"] = target
        if not refs:
            return None
        closure = store.graph_closure(list(refs.values()))
        objects = [
            {
                "id": object_id,
                "kind": obj.kind,
                "schema": obj.schema,
                "data": obj.data,
                "links": list(obj.links),
            }
            for object_id, obj in closure.items()
        ]
        derivations: list[dict[str, Any]] = []
        seen: set[str] = set()
        for object_id in closure:
            for row in store.derivation_records_for_output(object_id):
                if row["id"] not in seen:
                    seen.add(row["id"])
                    derivations.append(row)
        return {"objects": objects, "derivations": derivations, "refs": refs}
    finally:
        store.close()


def import_bundle(payload: dict[str, Any]) -> dict[str, int]:
    """Import a bundle, returning records/objects/sessions counts.

    Already-indexed records are skipped, so re-importing the same
    bundle neither bloats the event log nor changes any answer.
    """
    if payload.get("sigil_bundle") != BUNDLE_VERSION:
        raise ValueError(f"not a sigil bundle (expected version {BUNDLE_VERSION})")
    records = import_ledger_records(
        default_ledger_index(), payload.get("records") or []
    )
    objects = 0
    sessions = payload.get("sessions") or {}
    for session_id, graph in sessions.items():
        objects += import_session_graph(session_id, graph)
    return {"records": records, "objects": objects, "sessions": len(sessions)}


def import_ledger_records(
    index: LedgerIndex,
    records: list[dict[str, Any]],
) -> int:
    """Append new turn/effect records to the event log and index them."""
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True)
    log_path = root / EVENT_LOG_NAME
    imported = 0
    for record in records:
        if not isinstance(record, dict) or not new_ledger_record(index, record):
            continue
        rotate_oversized_log(log_path)
        append_jsonl_line(log_path, record)
        index.index_record(record)
        imported += 1
    return imported


def new_ledger_record(index: LedgerIndex, record: dict[str, Any]) -> bool:
    """Return whether a record is an importable, not-yet-indexed one."""
    if is_turn_record(record):
        return index.turn(str(record.get("turn_id") or "")) is None
    if is_effect_record(record):
        return index.effect(str(record.get("effect_id") or "")) is None
    return False


def import_session_graph(session_id: str, graph: dict[str, Any]) -> int:
    """Write one session's exported objects, derivations, and refs."""
    store = SqliteStore(session_sqlite_path(session_id))
    count = 0
    try:
        with store.batch():
            for entry in graph.get("objects") or []:
                store.import_object(
                    str(entry["id"]),
                    Object(
                        kind=str(entry["kind"]),
                        schema=str(entry["schema"]),
                        data=entry["data"],
                        links=tuple(entry["links"]),
                    ),
                )
                count += 1
            for row in graph.get("derivations") or []:
                store.import_derivation(
                    str(row["id"]),
                    Derivation(
                        producer=str(row["producer"]),
                        output_id=str(row["output_id"]),
                        input_ids=tuple(row["input_ids"]),
                        params=row["params"],
                    ),
                    float(row["created_at"]),
                )
            for name, object_id in (graph.get("refs") or {}).items():
                store.set_ref(str(name), str(object_id))
    finally:
        store.close()
    return count
