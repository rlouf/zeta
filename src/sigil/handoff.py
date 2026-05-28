"""State plumbing for Pi bash tool handoff.

Pi can propose a bash command through a tool call. Sigil blocks that execution
in a Pi extension, records the proposed command here, and lets the shell binding
put the command back under the user's cursor.
"""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from .security import create_trust_metadata, normalize_labels, normalize_mode
from .state import append_event, read_jsonl, session_dir, write_jsonl

PENDING_BASH_HANDOFF_FILE = "pending-bash-handoff.jsonl"
LAST_BASH_HANDOFF_FILE = "last-bash-handoff.jsonl"
BASH_HANDOFF_EXTENSION = "pi_extensions/bash_handoff.ts"


def bash_handoff_extension_path() -> Path | None:
    """Return the bundled Pi extension path if it is available on disk."""
    resource = files("sigil").joinpath(BASH_HANDOFF_EXTENSION)
    if not resource.is_file():
        return None
    return Path(str(resource))


def pending_bash_handoff_path() -> Path:
    """Return the path passed to Pi's handoff extension."""
    return session_dir() / PENDING_BASH_HANDOFF_FILE


def prepare_bash_handoff() -> Path:
    """Clear stale handoff state and return the pending handoff path."""
    root = session_dir()
    root.mkdir(parents=True, exist_ok=True)
    for name in (PENDING_BASH_HANDOFF_FILE, LAST_BASH_HANDOFF_FILE):
        path = root / name
        if path.exists():
            path.unlink()
    return root / PENDING_BASH_HANDOFF_FILE


def read_pending_bash_handoffs() -> list[dict[str, Any]]:
    """Read raw handoff records written by the Pi extension."""
    path = pending_bash_handoff_path()
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def record_bash_handoffs(
    *,
    source_event: dict[str, Any],
    source_security: dict[str, Any],
) -> list[dict[str, Any]]:
    """Promote raw handoff records into trusted Sigil session state."""
    records = []
    source_event_id = str(source_event.get("id") or "")
    glyph = str(source_security.get("glyph") or "?")
    labels = normalize_labels(source_security.get("labels"))
    mode = normalize_mode(source_security.get("mode"))

    for raw in read_pending_bash_handoffs():
        command = str(raw.get("command") or "").strip()
        if not command:
            continue
        security = create_trust_metadata(
            glyph=glyph,
            mode=mode if mode == "propose" else "propose",
            labels=labels,
            inputs=[source_event_id] if source_event_id else [],
            input_records=[source_event],
        )
        event = append_event(
            {
                "type": "bash_handoff",
                "command": command,
                "tool_call_id": raw.get("toolCallId"),
                "source": "pi_tool_call",
                **security,
            }
        )
        records.append(
            {
                "event_id": event["id"],
                "command": command,
                "tool_call_id": raw.get("toolCallId"),
                "source": "pi_tool_call",
                **security,
            }
        )

    write_jsonl(LAST_BASH_HANDOFF_FILE, records)
    pending = pending_bash_handoff_path()
    if pending.exists():
        pending.unlink()
    return records


def latest_bash_handoff() -> dict[str, Any] | None:
    """Return the latest command handed off from Pi, if any."""
    records = read_jsonl(LAST_BASH_HANDOFF_FILE)
    if not records:
        return None
    return records[-1]


def consume_latest_bash_handoff() -> dict[str, Any] | None:
    """Return and clear the latest bash handoff command."""
    record = latest_bash_handoff()
    write_jsonl(LAST_BASH_HANDOFF_FILE, [])
    return record
