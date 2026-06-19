"""Session continuity state: inspection helpers and shell turn recording.

Session state is useful only if it can be inspected. These helpers make the
current shell's continuity files visible, and own the recent-turns buffer the
shell bindings write through `record_turn`.
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from sigil.protocols import (
    EFFECT_KIND_COMMAND,
    TURN_OUTCOME_EXECUTED,
    TURN_OUTCOME_FAILED,
    turn_contract,
)
from sigil.state import event_store_path, read_events, state_dir
from zeta.history import (
    effect_record,
    publish_effect_record,
    publish_turn_record,
    turn_record,
)
from zeta.kernel.events import Event

RUN_WORKFLOW = "run"

SESSION_FILES = (
    "session.json",
    "last-failure.json",
    "recent-turns.jsonl",
)

SESSION_METADATA_FILE = "session.json"
RECENT_TURNS_FILE = "recent-turns.jsonl"
RECENT_TURNS_LIMIT = 50
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
TURN_SKIP_PREFIXES = (",", "sigil ", "__sigil_")


def _session_path_component(raw: str) -> str:
    """Map raw session ids into one safe filesystem component."""
    if SESSION_ID_PATTERN.fullmatch(raw) and raw not in {".", ".."}:
        return raw
    return "unsafe-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def session_id() -> str:
    """Return the current shell session identifier."""
    return _session_path_component(os.environ.get("SIGIL_SESSION_ID") or "default")


def session_dir(session_id: str | None = None) -> Path:
    """Return the continuity directory for one shell session."""
    if session_id is None:
        base = os.environ.get("SIGIL_SESSION_DIR")
        if base:
            return Path(base)
    raw = session_id or os.environ.get("SIGIL_SESSION_ID") or "default"
    return state_dir() / "sessions" / _session_path_component(raw)


def write_text_atomic(path: Path, text: str) -> None:
    """Replace session files without exposing partial writes to readers."""
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f"{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(f.name, path)


def _session_root() -> Path:
    """Return the session directory, creating it if needed."""
    root = session_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


def write_json(name: str, value: Any) -> None:
    """Atomically write a session-scoped JSON document."""
    write_text_atomic(
        _session_root() / name, json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    )


def read_json(name: str) -> Any | None:
    """Read a session-scoped JSON document if it exists and parses."""
    path = session_dir() / name
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return value


def append_jsonl_line(path: Path, payload: dict[str, Any]) -> None:
    """Append one JSONL payload as a single unbuffered write.

    Concurrent shells append to the same files; one write(2) call per line
    keeps lines from interleaving regardless of payload size.
    """
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    with path.open("ab", buffering=0) as f:
        f.write(line.encode("utf-8"))


def read_jsonl(name: str) -> list[dict[str, Any]]:
    """Read a session-scoped JSONL file, skipping malformed lines."""
    path = session_dir() / name
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except Exception:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def session_paths() -> dict[str, str]:
    """Return the global and current-session paths users need for debugging."""
    return {
        "state": str(state_dir()),
        "session": str(session_dir()),
        "session_id": session_id(),
        "events": str(event_store_path()),
    }


def known_sessions() -> list[dict[str, Any]]:
    """List session directories with coarse file presence and event metadata."""
    root = state_dir() / "sessions"
    if not root.exists():
        return []
    latest_by_session = latest_events_by_session(read_events())
    sessions = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if not path.is_dir():
            continue
        latest = latest_by_session.get(path.name)
        metadata = session_metadata(path)
        sessions.append(
            {
                "session_id": path.name,
                "name": metadata.get("name"),
                "path": str(path),
                "files": sorted(item.name for item in path.iterdir() if item.is_file()),
                "last_event_time": event_time(latest) if latest is not None else None,
                "last_event_type": latest.event_type if latest is not None else None,
                "last_cwd": latest.payload.get("cwd") if latest is not None else None,
            }
        )
    return sessions


def session_metadata(path: Path) -> dict[str, Any]:
    """Return display metadata for one session directory."""
    value = read_session_file(path / SESSION_METADATA_FILE)
    if not isinstance(value, dict):
        return {}
    name = value.get("name")
    if not isinstance(name, str) or not name:
        return {}
    return {"name": name}


def latest_events_by_session(events: list[Event]) -> dict[str, Event]:
    """Return the newest event seen for each session id."""
    latest: dict[str, Event] = {}
    for event in events:
        if event.session_id is None:
            continue
        current = latest.get(event.session_id)
        if current is None or event_time(event) >= event_time(current):
            latest[event.session_id] = event
    return latest


def event_time(event: Event | dict[str, Any]) -> float:
    """Return event time as a sortable float, treating malformed times as zero."""
    if isinstance(event, Event):
        return event.timestamp_ms / 1_000
    value = event.get("time")
    return value if isinstance(value, int | float) else 0.0


def read_session_file(path: Path) -> Any:
    """Read a session file as JSON, JSONL, or text for display."""
    if not path.exists():
        return None
    if path.suffix == ".jsonl":
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                rows.append({"malformed": line})
        return rows
    if path.suffix == ".json":
        text = path.read_text(encoding="utf-8")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return path.read_text(encoding="utf-8")


def current_session_snapshot() -> dict[str, Any]:
    """Return the current session's continuity files as structured data."""
    root = session_dir()
    metadata = session_metadata(root)
    return {
        "session_id": session_id(),
        "name": metadata.get("name"),
        "path": str(root),
        "files": {name: read_session_file(root / name) for name in SESSION_FILES},
    }


def rename_current_session(name: str) -> dict[str, Any]:
    """Set a human display name for the current session without changing its id."""
    clean_name = " ".join(name.split())
    root = session_dir()
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": clean_name,
        "renamed_at": time.time(),
    }
    write_text_atomic(
        root / SESSION_METADATA_FILE,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )
    return {"session_id": session_id(), "name": clean_name, "path": str(root)}


def clear_current_session() -> dict[str, list[str]]:
    """Clear state scoped to the current session."""
    from sigil import zeta_session_for_sigil

    removed: list[str] = []
    root = session_dir()
    if root.exists():
        removed = [str(path) for path in sorted(root.rglob("*")) if path.is_file()]
        shutil.rmtree(root)

    cleared: list[str] = []
    context = zeta_session_for_sigil()
    trace_store = context.trace_store
    if hasattr(trace_store, "clear_session"):
        trace_store.clear_session(session_id())  # type: ignore[attr-defined]
        path = getattr(trace_store, "path", None)
        if path is not None:
            cleared.append(str(path))
    cleared.extend(clear_zeta_event_continuity(context.event_sink))
    return {"removed": removed, "cleared": sorted(set(cleared))}


def clear_zeta_event_continuity(event_sink: Any) -> list[str]:
    if not hasattr(event_sink, "clear_session_events"):
        return []
    event_sink.clear_session_events(  # type: ignore[attr-defined]
        session_id(),
        event_type_prefix="zeta.",
    )
    path = getattr(event_sink, "path", None)
    return [str(path)] if path is not None else []


RECENT_TURNS_PROMPT_LIMIT = 10
RECENT_TURN_LINE_CHARS = 120
RECENT_TURN_SNIPPET_CHARS = 500


def recent_turns(limit: int = RECENT_TURNS_LIMIT) -> list[dict[str, Any]]:
    """Return the most recent shell turns recorded by the bindings."""
    rows = read_jsonl(RECENT_TURNS_FILE)
    if limit < len(rows):
        return rows[-limit:]
    return rows


def latest_active_failure() -> dict[str, Any] | None:
    """Return the last failure only when it is still the latest shell turn."""
    from sigil.failure import last_failure_or_none

    failure = last_failure_or_none()
    if failure is None:
        return None
    turns = recent_turns(limit=1)
    if not turns:
        return failure
    status = turns[-1].get("status")
    if isinstance(status, int) and status != 0:
        return failure
    return None


def active_failure_context(since: float | None = None) -> str:
    """Return last-failure context when the latest shell command failed.

    A failure older than ``since`` returns nothing: the model already saw it.
    """
    from sigil.failure import failure_context_prompt

    failure = latest_active_failure()
    if failure is None:
        return ""
    if since is not None and event_time(failure) <= since:
        return ""
    return "Last failed command context:\n" + failure_context_prompt(failure)


def recent_turns_context(
    limit: int = RECENT_TURNS_PROMPT_LIMIT,
    since: float | None = None,
) -> str:
    """Return a compact summary of shell turns newer than ``since``, if any."""
    turns = recent_turns(limit=limit)
    if since is not None:
        turns = [turn for turn in turns if event_time(turn) > since]
    if not turns:
        return ""
    lines = ["Recent shell activity:"]
    for turn in turns:
        command = str(turn.get("command", "")).rstrip("\n")
        if len(command) > RECENT_TURN_LINE_CHARS:
            command = command[:RECENT_TURN_LINE_CHARS] + "…"
        status = turn.get("status", "?")
        lines.append(f"  {command} (exit {status})")
        stderr = compact_turn_snippet(turn.get("stderr_snippet"))
        stdout = compact_turn_snippet(turn.get("stdout_snippet"))
        if stderr:
            lines.append(f"    stderr: {stderr}")
        if stdout:
            lines.append(f"    stdout: {stdout}")
    return "\n".join(lines)


def compact_turn_snippet(value: object) -> str:
    """Return one compact line of captured command output."""
    if not isinstance(value, str) or not value:
        return ""
    text = " ".join(value.split())
    if len(text) <= RECENT_TURN_SNIPPET_CHARS:
        return text
    return text[-RECENT_TURN_SNIPPET_CHARS:]


def turn_is_skippable(command: str) -> bool:
    """Return True for commands the per-turn buffer should ignore."""
    if not command or not command.strip():
        return True
    if command[0].isspace():
        return True
    for prefix in TURN_SKIP_PREFIXES:
        if command.startswith(prefix):
            return True
    return False


def record_turn(
    command: str,
    status: int,
    cwd: str | None = None,
    stdout_snippet: str | None = None,
    stderr_snippet: str | None = None,
    duration_ms: int | None = None,
    at: float | None = None,
) -> None:
    """Persist one shell turn and fan out to failure recording on non-zero exit.

    ``at`` carries the original timestamp for turns recorded after the fact,
    such as spooled binding records; live recording stamps the current time.
    """
    from sigil.failure import record_failure, truncate_snippet

    if turn_is_skippable(command):
        return

    turn_cwd = cwd or os.getcwd()
    stdout_text = truncate_snippet(stdout_snippet)
    stderr_text = truncate_snippet(stderr_snippet)
    entry = {
        "id": str(uuid.uuid4()),
        "time": time.time() if at is None else at,
        "session": session_id(),
        "command": command,
        "status": status,
        "turn_cwd": turn_cwd,
        "glyph": "turn",
    }
    if stdout_text:
        entry["stdout_snippet"] = stdout_text
    if stderr_text:
        entry["stderr_snippet"] = stderr_text
    _append_recent_turn(entry)
    record_run_history(command, status, turn_cwd, duration_ms)

    if status != 0:
        record_failure(
            command,
            status,
            turn_cwd,
            stdout_snippet=stdout_text,
            stderr_snippet=stderr_text,
        )


def record_run_history(
    command: str,
    status: int,
    cwd: str,
    duration_ms: int | None,
) -> None:
    """Append the run-workflow turn and command effect for one shell command."""
    turn_id = str(uuid.uuid4())
    effect_id = str(uuid.uuid4())
    publish_effect_record(
        effect_record(
            effect_id,
            turn_id=turn_id,
            kind=EFFECT_KIND_COMMAND,
            staged=False,
            command=command,
            exit_status=status,
            duration_ms=duration_ms,
        ),
        path=event_store_path(),
        session_id=session_id(),
    )
    turn = turn_record(
        turn_id,
        workflow=RUN_WORKFLOW,
        objective=command,
        contract=turn_contract(RUN_WORKFLOW, (), staged=False),
        outcome=TURN_OUTCOME_EXECUTED if status == 0 else TURN_OUTCOME_FAILED,
        effect_ids=[effect_id],
    )
    turn["cwd"] = cwd
    publish_turn_record(turn, path=event_store_path(), session_id=session_id())


SHELL_TURN_SPOOL_FILE = "shell-turns.spool"
SPOOL_FIELD_SEPARATOR = "\x1f"
SPOOL_RECORD_SEPARATOR = "\x1e"
SPOOL_ORPHAN_AGE_SECONDS = 60.0


def ingest_spooled_turns() -> int:
    """Record shell turns spooled by the bindings; return how many landed.

    The binding appends raw spool records with zero subprocess cost; every
    CLI invocation calls this matching reader, so spooled turns are recorded
    before anything reads recent turns or failure context. The spool is
    claimed by rename, which keeps concurrent CLI processes from
    double-recording the same records.
    """
    recorded = 0
    for path in _claimed_spool_files():
        recorded += _record_spool_text(
            path.read_text(encoding="utf-8", errors="replace")
        )
        path.unlink(missing_ok=True)
    return recorded


def _claimed_spool_files() -> list[Path]:
    root = session_dir()
    spool = root / SHELL_TURN_SPOOL_FILE
    claim = spool.with_name(f"{spool.name}.{os.getpid()}.ingesting")
    claimed: list[Path] = []
    try:
        spool.rename(claim)
        claimed.append(claim)
    except OSError:
        pass
    for orphan in root.glob(f"{SHELL_TURN_SPOOL_FILE}.*.ingesting"):
        if orphan == claim:
            continue
        try:
            age = time.time() - orphan.stat().st_mtime
        except OSError:
            continue
        if age > SPOOL_ORPHAN_AGE_SECONDS:
            claimed.append(orphan)
    return claimed


def _record_spool_text(text: str) -> int:
    recorded = 0
    for record in text.split(SPOOL_RECORD_SEPARATOR):
        fields = record.split(SPOOL_FIELD_SEPARATOR)
        if len(fields) != 4:
            continue
        raw_time, command, raw_status, cwd = fields
        try:
            status = int(raw_status)
        except ValueError:
            continue
        try:
            at = float(raw_time)
        except ValueError:
            at = time.time()
        record_turn(command, status, cwd, at=at)
        recorded += 1
    return recorded


def _append_recent_turn(entry: dict[str, Any]) -> None:
    root = session_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / RECENT_TURNS_FILE
    append_jsonl_line(path, entry)
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    if len(lines) > RECENT_TURNS_LIMIT:
        write_text_atomic(path, "\n".join(lines[-RECENT_TURNS_LIMIT:]) + "\n")
