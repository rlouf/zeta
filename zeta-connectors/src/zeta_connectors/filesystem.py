"""The filesystem connector: watch directories, emit `file.created`.

A per-directory watermark (the time of the first poll) plus a
seen-set means existing files are never re-emitted and the watcher
cannot flood downstream agents; it stays correct if a directory only
becomes readable later.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from zeta.wire.plugin import EventType, SourceEvent, run_source

from zeta_connectors import connector_main

FILE_CREATED = "file.created"
DEFAULT_POLL_INTERVAL_SECS = 1.0


def file_created_schema() -> dict[str, Any]:
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


def filesystem_filter_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["dir"],
        "properties": {
            "dir": {"type": "string"},
            "glob": {"type": "string"},
        },
        "additionalProperties": False,
    }


MANIFEST: dict[str, Any] = {
    "id": "filesystem",
    "protocol_versions": [0],
    "events": {FILE_CREATED: file_created_schema()},
    "filters": {FILE_CREATED: filesystem_filter_schema()},
    "operations": [],
    "settings": ["bindings", "poll_interval", "debounce"],
}


def collect_file_created(
    watch: dict[str, Any],
    state: dict[str, dict[str, Any]],
    *,
    now: float,
    debounce_seconds: float,
) -> list[dict[str, Any]]:
    """Return payloads for files that appeared under one watch."""
    directory = str(watch.get("dir") or "")
    if not directory:
        return []
    pattern = str(watch.get("glob") or "*")
    root = Path(directory).expanduser()

    dir_state = state.setdefault(directory, {"since": None, "seen": {}})
    seen: dict[str, float] = dir_state["seen"]
    if dir_state["since"] is None:
        dir_state["since"] = now
    since: float = dir_state["since"]

    payloads: list[dict[str, Any]] = []
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
        payloads.append({"path": key, "name": path.name, "dir": str(root)})
    return payloads


def _matches(root: Path, pattern: str) -> list[Path]:
    try:
        return sorted(p for p in root.glob(pattern) if p.is_file())
    except OSError:
        return []  # directory absent or unreadable


def watch_events(config: dict[str, Any]) -> AsyncIterator[SourceEvent] | None:
    watches = [
        dict(binding.get("filter", {}))
        for binding in config.get("bindings", [])
        if binding.get("event") == FILE_CREATED
    ]
    if not watches:
        return None
    poll_interval = float(config.get("poll_interval", DEFAULT_POLL_INTERVAL_SECS))
    debounce = config.get("debounce")
    if debounce is None:
        debounce = float(os.environ.get("FILESYSTEM_DEBOUNCE_SECONDS", "2"))

    async def events() -> AsyncIterator[SourceEvent]:
        state: dict[str, dict[str, Any]] = {}
        while True:
            now = time.time()
            for watch in watches:
                for payload in collect_file_created(
                    watch, state, now=now, debounce_seconds=float(debounce)
                ):
                    yield SourceEvent(FILE_CREATED, payload)
            await asyncio.sleep(poll_interval)

    return events()


def run() -> None:
    run_source(
        watch_events,
        name="filesystem",
        plugin_version=MANIFEST_VERSION,
        event_types=[EventType(FILE_CREATED, f"{FILE_CREATED}@1")],
    )


MANIFEST_VERSION = "0.1.0"


def main(argv: list[str] | None = None) -> None:
    connector_main(
        sys.argv[1:] if argv is None else argv,
        manifest=MANIFEST,
        run=run,
    )


if __name__ == "__main__":
    main()
