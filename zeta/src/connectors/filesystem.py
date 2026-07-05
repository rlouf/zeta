"""Filesystem event connector.

Polls a directory and emits ``file.created`` for files that appear after the
connector starts watching. A per-directory watermark (the time of the first
poll) plus a seen-set means existing files are never re-emitted and the watcher
cannot flood downstream agents — the same approach the Voice Memos collector
uses, and it stays correct if a directory only becomes readable later.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from zeta.events import DraftEvent

from connectors import EventConnector, IngressBinding, IngressInput

FILE_CREATED = "file.created"


def filesystem_event_connector(
    *,
    now: Any | None = None,
    debounce_seconds: float | None = None,
) -> EventConnector:
    clock = now or time.time
    debounce = (
        debounce_seconds
        if debounce_seconds is not None
        else float(os.environ.get("FILESYSTEM_DEBOUNCE_SECONDS", "2"))
    )
    state: dict[str, dict[str, Any]] = {}

    def ingress(
        binding: IngressBinding, item: IngressInput = None
    ) -> tuple[DraftEvent, ...]:
        return collect_file_created(
            binding, state, now=clock(), debounce_seconds=debounce
        )

    return EventConnector(
        id="filesystem",
        events={FILE_CREATED: file_created_schema()},
        ingress={FILE_CREATED: ingress},
        filters={FILE_CREATED: filesystem_filter_schema()},
    )


def collect_file_created(
    binding: IngressBinding,
    state: dict[str, dict[str, Any]],
    *,
    now: float,
    debounce_seconds: float,
) -> tuple[DraftEvent, ...]:
    directory = str(binding.filter.get("dir") or "")
    if not directory:
        return ()
    pattern = str(binding.filter.get("glob") or "*")
    root = Path(directory).expanduser()

    dir_state = state.setdefault(directory, {"since": None, "seen": {}})
    seen: dict[str, float] = dir_state["seen"]
    if dir_state["since"] is None:
        dir_state["since"] = now
    since: float = dir_state["since"]

    drafts: list[DraftEvent] = []
    for path in _matches(root, pattern):
        key = str(path)
        if key in seen:
            continue
        mtime = path.stat().st_mtime
        if mtime <= since:
            continue  # existed before we started watching this directory
        if now - mtime < debounce_seconds:
            continue  # still being written; revisit next poll
        seen[key] = mtime
        drafts.append(
            DraftEvent(
                FILE_CREATED,
                "filesystem",
                {"path": key, "name": path.name, "dir": str(root)},
            )
        )
    return tuple(drafts)


def _matches(root: Path, pattern: str) -> list[Path]:
    try:
        return sorted(p for p in root.glob(pattern) if p.is_file())
    except OSError:
        return []  # directory absent or unreadable


def file_created_schema() -> Mapping[str, Any]:
    return {
        "type": "object",
        "required": ["path", "name", "dir"],
        "properties": {
            "path": {"type": "string"},
            "name": {"type": "string"},
            "dir": {"type": "string"},
        },
        "additionalProperties": False,
    }


def filesystem_filter_schema() -> Mapping[str, Any]:
    return {
        "type": "object",
        "required": ["dir"],
        "properties": {
            "dir": {"type": "string"},
            "glob": {"type": "string"},
        },
        "additionalProperties": False,
    }
