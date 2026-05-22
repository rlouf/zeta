from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base) / "sigil"
    return Path.home() / ".local" / "state" / "sigil"


def append_event(event: dict[str, Any]) -> None:
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": str(uuid.uuid4()),
        "time": time.time(),
        "cwd": os.getcwd(),
        **event,
    }
    with (root / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def write_json(name: str, value: Any) -> None:
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f"{name}.tmp"
    final = root / name
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(final)


def read_json(name: str) -> Any | None:
    path = state_dir() / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

