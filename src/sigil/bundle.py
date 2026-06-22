"""Portable turn-history bundles."""

from typing import Any

from sigil.state import event_store_path, history_view
from zeta.records.stores import export_trace_refs, import_trace_graph
from zeta.records.timeline import import_history_records

BUNDLE_VERSION = 1


def export_bundle(
    *,
    since: float | None = None,
    session: str | None = None,
) -> dict[str, Any]:
    """Collect matching turns, their effects, and their trace closures."""
    history = history_view()
    records: list[dict[str, Any]] = []
    turn_ids_by_session: dict[str, list[str]] = {}
    for turn in history.query_turns(session=session, since=since):
        turn_id = str(turn.get("turn_id") or "")
        records.append(turn)
        records.extend(history.effects_for_turn(turn_id))
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
    still exports its history records; only the graph section is absent.
    """
    return export_trace_refs(session_id, [f"turn/{turn_id}" for turn_id in turn_ids])


def import_bundle(payload: dict[str, Any]) -> dict[str, int]:
    """Import a bundle, returning records/objects/sessions counts."""
    if payload.get("sigil_bundle") != BUNDLE_VERSION:
        raise ValueError(f"not a sigil bundle (expected version {BUNDLE_VERSION})")
    records = import_history_records(
        history_view(),
        payload.get("records") or [],
        path=event_store_path(),
    )
    objects = 0
    sessions = payload.get("sessions") or {}
    for session_id, graph in sessions.items():
        objects += import_session_graph(session_id, graph)
    return {"records": records, "objects": objects, "sessions": len(sessions)}


def import_session_graph(session_id: str, graph: dict[str, Any]) -> int:
    """Write one session's exported objects, derivations, and refs."""
    return import_trace_graph(session_id, graph)
