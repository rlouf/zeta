import ast
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stderr
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any, cast

import click
import pytest
from click.testing import CliRunner
from commas.cli import cli, main
from commas.cli._base import (
    EXIT_COMMAND_NOT_FOUND,
    EXIT_ERROR,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_SIGNAL_BASE,
    EXIT_USAGE,
)
from commas.display.tty import should_color
from commas.failure import failure_context_prompt, record_failure, truncate_snippet
from commas.sessions import (
    ingest_spooled_turns,
    recent_turns,
    recent_turns_context,
    record_turn,
    session_dir,
    session_id,
)
from commas.state import (
    append_event,
    causal_chain,
    event_children,
    events_for_turn,
    state_dir,
)
from commas.workflows.ask import (
    ASK_SYSTEM_PROMPT,
    ask,
)
from zeta.records import events as zeta_events
from zeta.records import events as zeta_kernel_events
from zeta.records.events import (
    AppendOutcome,
    DraftEvent,
    Event,
    event_view,
    publish_event,
)
from zeta.records.stores.event_store import EventStoreProtocol, Filter
from zeta.records.stores.memory import MemoryEventStore
from zeta.records.stores.sqlite import (
    SqliteEventStore,
    event_store_path,
)
from zeta.run import runs as zeta_kernel_runs
from zetad import dispatch as zetad_dispatch
from zetad.attempts import (
    Attempt,
    attempt_event_payload,
    attempt_from_event_payload,
)
from zetad.cli import cli as zeta_cli
from zetad.queue import (
    QueueItem,
    queue_item_event_payload,
    queue_item_from_event_payload,
)

from test_support.patch import patch, patch_dict
from test_support.zeta_helpers import record_durable_timeline_event


class TtyStringIO(StringIO):
    def isatty(self) -> bool:
        return True


def test_zeta_package_does_not_import_parent_commas_modules() -> None:
    zeta_root = Path("zeta/src/zeta")
    violations: list[str] = []
    for path in sorted(zeta_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        package_parts = ("zeta", *path.relative_to(zeta_root).parts[:-1])
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "commas" or alias.name.startswith("commas."):
                        violations.append(f"{path}:{node.lineno}: import {alias.name}")
            if isinstance(node, ast.ImportFrom):
                resolved = resolved_import_module(package_parts, node)
                if resolved and resolved[0] == "commas":
                    module = "." * node.level + (node.module or "")
                    violations.append(f"{path}:{node.lineno}: from {module}")
    assert violations == []


def test_zeta_package_does_not_import_zetad_modules() -> None:
    zeta_root = Path("zeta/src/zeta")
    violations: list[str] = []
    for path in sorted(zeta_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        package_parts = ("zeta", *path.relative_to(zeta_root).parts[:-1])
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "zetad" or alias.name.startswith("zetad."):
                        violations.append(f"{path}:{node.lineno}: import {alias.name}")
            if isinstance(node, ast.ImportFrom):
                resolved = resolved_import_module(package_parts, node)
                if resolved and resolved[0] == "zetad":
                    module = "." * node.level + (node.module or "")
                    violations.append(f"{path}:{node.lineno}: from {module}")
    assert violations == []


def test_zeta_events_exports_the_canonical_event_boundary() -> None:
    deleted_compatibility_names = {
        "current_timeline",
        "record_event",
        "last_event_time",
        "durable_event",
        "model_called_event",
        "tool_called_event",
        "event_payload_draft",
        "timeline_event_from_durable_event",
    }

    assert deleted_compatibility_names.isdisjoint(set(zeta_events.__all__))
    assert {"event_view", "draft_event_view"}.issubset(set(zeta_events.__all__))
    assert zeta_kernel_events.DraftEvent is DraftEvent
    assert zeta_kernel_events.Event is Event
    assert not hasattr(zeta_kernel_events, "EventFilter")
    assert zeta_kernel_runs.Run(run_id="run_1", status="running").run_id == "run_1"


def test_zeta_record_stores_package_does_not_reexport_store_symbols() -> None:
    import zeta.records.stores as stores

    reexported_names = {
        "AppendOutcome",
        "EventReader",
        "EventStoreProtocol",
        "Filter",
        "InMemoryStore",
        "MemoryEventStore",
        "SqliteEventStore",
        "SqliteObjectStore",
        "Store",
        "QueueClaim",
        "SqliteStore",
    }

    assert reexported_names.isdisjoint(dir(stores))
    assert not hasattr(stores, "__all__")


def test_zeta_capabilities_package_does_not_reexport_leaf_symbols() -> None:
    import zeta.capabilities as capabilities

    reexported_names = {
        "Capability",
        "CapabilityError",
        "CapabilityExecutor",
        "CapabilityId",
        "CapabilityRegistry",
        "ExecutionMode",
        "InProcessCapabilityExecutor",
    }

    assert reexported_names.isdisjoint(dir(capabilities))
    if hasattr(capabilities, "registry"):
        assert (
            getattr(capabilities.registry, "__name__", "")
            == "zeta.capabilities.registry"
        )
    assert not hasattr(capabilities, "__all__")


def test_zeta_context_compaction_package_does_not_reexport_leaf_symbols() -> None:
    import zeta.context.compaction as compaction

    reexported_names = {
        "DropOldestPromptTransform",
        "StructuralTrimPromptTransform",
        "TASK_STATE_SCHEMA",
        "TaskStateExtractionPromptTransform",
        "TaskStateExtractor",
        "task_state_component",
    }

    assert reexported_names.isdisjoint(dir(compaction))
    assert not hasattr(compaction, "__all__")


def test_zetad_rpc_package_does_not_reexport_leaf_symbols() -> None:
    import zetad.rpc as rpc

    reexported_names = {
        "JsonRpcConnection",
        "JsonRpcRouter",
        "RpcClient",
        "RpcError",
        "RunState",
        "events_publish",
        "run_stdio",
        "session_run",
        "tools_register",
    }

    assert reexported_names.isdisjoint(dir(rpc))
    assert not hasattr(rpc, "__all__")


def test_zeta_models_package_exposes_only_high_level_gateway_api() -> None:
    import zeta.models as models

    assert set(models.__all__) == {
        "DefaultModelGateway",
        "chat_completion_messages",
        "chat_structured_output",
    }
    assert not hasattr(models, "ModelSelection")
    assert not hasattr(models, "resolve_active_model")
    assert not hasattr(models, "ModelInput")


def test_plain_sqlite_event_store_does_not_import_zetad_modules(
    tmp_path: Path,
) -> None:
    script = """
import sys
from pathlib import Path
from zeta.records.stores.sqlite import SqliteEventStore

store = SqliteEventStore(Path(sys.argv[1]) / "zeta.sqlite3")
store.close()
for module in ("zetad.queue", "zetad.attempts"):
    if module in sys.modules:
        raise SystemExit(f"{module} was imported")
"""
    subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        check=True,
        text=True,
    )


def test_plain_sqlite_event_store_does_not_expose_runtime_queue_api() -> None:
    runtime_api = {
        "ensure_pending_queue_item",
        "event_has_queue_item",
        "queue_item",
        "list_queue_items",
        "list_attempts",
        "heartbeat_attempt",
        "claim_next_queue_item",
        "release_queue_claim",
        "queue_claim_is_current",
        "reconcile_expired_queue_claims",
        "list_locks",
        "acquire_locks",
        "release_locks",
        "reconcile_expired_locks",
    }

    assert runtime_api.isdisjoint(dir(SqliteEventStore))


def test_zeta_dispatch_kernel_defines_queue_item_and_attempt_shapes() -> None:
    queue_item = QueueItem(
        queue_item_id="qi_evt_123_zeta_session_turn",
        event_id="evt_123",
        target_agent="zeta.session.turn",
        status="available",
    )
    attempt = Attempt(
        attempt_id="att_qi_evt_123_zeta_session_turn_1",
        queue_item_id=queue_item.queue_item_id,
        event_id=queue_item.event_id,
        attempt_number=1,
        target_agent=queue_item.target_agent,
        status="running",
        started_at="2026-06-20T10:00:01Z",
        run_id="run_123",
    )

    assert attempt.finished_at is None
    assert attempt.error is None
    assert attempt.session_id is None
    assert attempt.run_id == "run_123"
    assert "QueueItem" not in zetad_dispatch.__all__
    assert "Attempt" not in zetad_dispatch.__all__


def test_zeta_queue_item_runtime_payload_round_trips() -> None:
    queue_item = QueueItem(
        queue_item_id="qi_evt_123_zeta_session_turn",
        event_id="evt_123",
        target_agent="zeta.session.turn",
        status="completed",
    )

    payload = queue_item_event_payload(queue_item, result={"ok": True})

    assert payload == {
        "queue_item_id": "qi_evt_123_zeta_session_turn",
        "event_id": "evt_123",
        "target_agent": "zeta.session.turn",
        "status": "completed",
        "result": {"ok": True},
    }
    assert queue_item_from_event_payload(payload) == queue_item


def test_zeta_attempt_runtime_payload_round_trips() -> None:
    attempt = Attempt(
        attempt_id="att_qi_evt_123_zeta_session_turn_1",
        queue_item_id="qi_evt_123_zeta_session_turn",
        event_id="evt_123",
        attempt_number=1,
        target_agent="zeta.session.turn",
        status="completed",
        started_at="2026-06-20T10:00:01Z",
        finished_at="2026-06-20T10:00:02Z",
        session_id="session-1",
        run_id="run-123",
    )

    payload = attempt_event_payload(attempt, result={"ok": True})

    assert payload == {
        "attempt_id": "att_qi_evt_123_zeta_session_turn_1",
        "queue_item_id": "qi_evt_123_zeta_session_turn",
        "event_id": "evt_123",
        "attempt_number": 1,
        "target_agent": "zeta.session.turn",
        "status": "completed",
        "started_at": "2026-06-20T10:00:01Z",
        "finished_at": "2026-06-20T10:00:02Z",
        "error": None,
        "session_id": "session-1",
        "run_id": "run-123",
        "result": {"ok": True},
    }
    assert attempt_from_event_payload(payload) == attempt


def resolved_import_module(
    package_parts: tuple[str, ...],
    node: ast.ImportFrom,
) -> tuple[str, ...] | None:
    module_parts = tuple((node.module or "").split(".")) if node.module else ()
    if node.level == 0:
        return module_parts
    if node.level > len(package_parts):
        return ()
    return (*package_parts[: 1 - node.level], *module_parts)


def test_question_system_prompt_points_zeta_at_query_log_for_older_history() -> None:
    assert "use query_log" in ASK_SYSTEM_PROMPT
    assert (
        "available tools are read, grep, ls, query_log, and web_search only"
        in ASK_SYSTEM_PROMPT
    )


def test_top_level_help_lists_commands() -> None:
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == EXIT_OK
    assert "Common workflows:" in result.output
    assert ",      ask from local context" in result.output
    assert ",,     propose one reviewed agent step" in result.output
    assert ",,,    do one auto-approved agent step" in result.output
    assert "+      run one explicit command and capture output" in result.output
    assert "?      status for the current session" in result.output
    assert "named command:" not in result.output
    assert "named shell function:" not in result.output
    assert "Setup and diagnostics:" in result.output
    assert "commas doctor" in result.output
    assert "commas status" in result.output
    assert "Commands:" in result.output
    for command in [
        "ask",
        "doctor",
        "install",
        "session",
        "status",
    ]:
        assert f"\n  {command} " in result.output
    for command in [
        "command",
        "op",
        "record-turn",
        "record-failure",
        "run",
        "staged",
    ]:
        assert f"\n  {command} " not in result.output
    assert "\n  question" not in result.output


def test_top_level_without_command_shows_help() -> None:
    result = CliRunner().invoke(cli, [])
    assert result.exit_code == EXIT_OK
    assert "Common workflows:" in result.output
    assert "Commands:" in result.output


def _command_help_paths() -> list[tuple[str, ...]]:
    root = click.Context(cli)
    paths: list[tuple[str, ...]] = []

    def walk(group: click.Group, prefix: tuple[str, ...]) -> None:
        for name in group.list_commands(root):
            command = group.get_command(root, name)
            path = (*prefix, name)
            paths.append(path)
            if isinstance(command, click.Group):
                walk(command, path)

    walk(cli, ())
    return paths


@pytest.mark.parametrize("path", _command_help_paths(), ids="-".join)
def test_command_help_shows_examples(path: tuple[str, ...]) -> None:
    result = CliRunner().invoke(cli, [*path, "--help"])
    assert result.exit_code == EXIT_OK
    assert "Examples:" in result.output


def test_status_help_states_the_exit_contract() -> None:
    result = CliRunner().invoke(cli, ["status", "--help"])
    assert "Exits 1" in result.output


def test_ask_help_states_the_model_unavailable_exit() -> None:
    result = CliRunner().invoke(cli, ["ask", "--help"])
    assert "Exits 69" in result.output


def test_step_help_states_the_model_unavailable_exit() -> None:
    result = CliRunner().invoke(cli, ["step", "--help"])
    assert "Exits 69" in result.output


def test_doctor_help_states_the_exit_contract() -> None:
    result = CliRunner().invoke(cli, ["doctor", "--help"])
    assert "Exits 1" in result.output


def test_commas_trace_command_is_removed() -> None:
    result = CliRunner().invoke(cli, ["trace", "--help"])

    assert result.exit_code != 0
    assert "No such command" in result.output


HEAVY_MODULES_PROBE = (
    "heavy = [name for name in sys.modules if name.startswith('commas.workflows') "
    "or name.startswith('zeta') or name.startswith('rich')]; "
    "assert not heavy, heavy"
)


def test_cli_import_does_not_load_workflow_modules() -> None:
    script = "import sys; import commas.cli; " + HEAVY_MODULES_PROBE
    subprocess.run([sys.executable, "-c", script], check=True)


def test_status_dispatch_does_not_load_workflow_modules() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"}
        script = (
            "import sys; from commas.cli import main; "
            "code = main(['status']); "
            "assert code in (0, 1), code; "
            "heavy = [name for name in sys.modules "
            "if name.startswith('commas.workflows') or name.startswith('rich') "
            "or name in ('zeta.models.chat_completions', 'zeta.run.runtime', 'jsonschema')]; "
            "assert not heavy, heavy"
        )
        subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
        )


def test_spool_ingestion_does_not_load_display_or_model() -> None:
    # Every CLI start ingests the spool; the ingestion path must stay light
    # or glyph latency regresses for all commands at once.
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"}
        spool = Path(tmp) / "sessions" / "test" / "shell-turns.spool"
        spool.parent.mkdir(parents=True)
        spool.write_text("1700000000.0\x1fecho hi\x1f0\x1f/repo\x1e", encoding="utf-8")
        script = (
            "import sys; from commas.sessions import ingest_spooled_turns; "
            "count = ingest_spooled_turns(); "
            "assert count == 1, count; "
            "heavy = [name for name in sys.modules "
            "if name.startswith('commas.display') "
            "or name.startswith('zeta.run.runtime') "
            "or name.startswith('zeta.model') "
            "or name.startswith('rich')]; "
            "assert not heavy, heavy"
        )
        subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
        )


def test_model_selection_import_does_not_load_transport() -> None:
    script = (
        "import sys; "
        "import zeta.models; "
        "heavy = [name for name in sys.modules "
        "if name == 'zeta.models.chat_completions' or name == 'jsonschema']; "
        "assert not heavy, heavy"
    )
    subprocess.run([sys.executable, "-c", script], check=True)


def test_tty_helpers_do_not_load_display_renderer() -> None:
    script = (
        "import sys; "
        "import commas.display.tty; "
        "import zeta.models.chat_completions; "
        "heavy = [name for name in sys.modules "
        "if name == 'commas.display.render' or name.startswith('rich')]; "
        "assert not heavy, heavy"
    )
    subprocess.run([sys.executable, "-c", script], check=True)


def test_every_lazy_command_resolves() -> None:
    context = click.Context(cli)
    names = cli.list_commands(context)
    assert "ask" in names
    assert "doctor" in names
    for name in names:
        assert cli.get_command(context, name) is not None, name


def test_main_rewrites_missing_executable_errors() -> None:
    stderr = StringIO()
    missing = FileNotFoundError(2, "No such file or directory", "zeta")
    with patch("commas.cli.cli.main", side_effect=missing):
        with redirect_stderr(stderr):
            assert main(["ask", "hello"]) == EXIT_COMMAND_NOT_FOUND
    assert "missing executable: zeta" in stderr.getvalue()


def test_main_rewrites_permission_errors() -> None:
    stderr = StringIO()
    denied = PermissionError(1, "Operation not permitted", "/nope/zeta.sqlite3")
    with patch("commas.cli.cli.main", side_effect=denied):
        with redirect_stderr(stderr):
            assert main(["ask", "hello"]) == EXIT_ERROR
    assert "permission denied: /nope/zeta.sqlite3" in stderr.getvalue()


APPEND_LARGE_EVENTS_SCRIPT = """
import os
import sys
import time
from commas.state import append_event

marker, ready_path, start_path = sys.argv[1:4]
open(ready_path, "w").close()
while not os.path.exists(start_path):
    time.sleep(0.001)
for _ in range(25):
    append_event({"type": "big", "payload": marker * 65536})
"""


def test_event_store_records_large_events_across_processes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"}
        start_gate = Path(tmp) / "start"
        ready_gates = [Path(tmp) / "ready-a", Path(tmp) / "ready-b"]
        procs = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    APPEND_LARGE_EVENTS_SCRIPT,
                    marker,
                    str(ready),
                    str(start_gate),
                ],
                env=env,
            )
            for marker, ready in zip(("a", "b"), ready_gates, strict=True)
        ]
        deadline = time.monotonic() + 30
        while not all(gate.exists() for gate in ready_gates):
            assert time.monotonic() < deadline
            time.sleep(0.001)
        start_gate.touch()
        for proc in procs:
            assert proc.wait(timeout=60) == 0
        store = SqliteEventStore(Path(tmp) / "zeta.sqlite3")
        events = store.list_events(Filter(event_type="big"))

    assert len(events) == 50
    for event in events:
        payload = event.payload["payload"]
        assert set(payload) in ({"a"}, {"b"})


def test_event_store_path_uses_zeta_state_dir() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp},
        ):
            path = event_store_path()

    assert path == Path(tmp) / "zeta.sqlite3"


def test_commas_default_state_matches_zeta_events_default_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("ZETA_STATE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("COMMAS_SESSION_ID", "test")
    monkeypatch.chdir(tmp_path)

    record_turn("echo hi", 0, str(tmp_path))

    result = CliRunner().invoke(zeta_cli, ["events", "--json"])

    assert result.exit_code == 0
    events = json.loads(result.output)
    assert events
    assert {event["type"] for event in events} >= {
        "zeta.tool_call.completed",
        "zeta.turn.completed",
    }
    assert state_dir() == home / ".zeta"


def test_sqlite_event_store_deduplicates_idempotency_keys(tmp_path: Path) -> None:
    store = SqliteEventStore(tmp_path / "zeta.sqlite3")
    draft = DraftEvent(
        event_type="zeta.turn.completed",
        source="test",
        payload={"turn_id": "turn-1"},
        idempotency_key="turn:turn-1",
        session_id="s1",
        turn_id="turn-1",
    )

    first = store.accept(draft)
    second = store.accept(draft)

    assert first.inserted is True
    assert second.inserted is False
    assert second.event == first.event
    assert [event.id for event in store.list_events(Filter())] == [first.event.id]
    assert first.event.turn_id == "turn-1"
    assert first.event.payload == {"turn_id": "turn-1"}
    assert first.event.cursor == second.event.cursor


def test_event_from_draft_uses_uuid_id_and_normalizes_idempotency_key() -> None:
    payload = {"run_id": "run-1"}
    event = Event.from_draft(
        DraftEvent(
            event_type="zeta.user_message",
            source="test",
            payload=payload,
            idempotency_key=" user:run-1 ",
            run_id="run-1",
        )
    )
    payload["run_id"] = "mutated"

    assert event.id.startswith("evt_")
    assert len(event.id) == 36
    assert event.idempotency_key == "user:run-1"
    assert event.payload == {"run_id": "run-1"}
    assert event.run_id == "run-1"


def test_event_stores_return_payload_snapshots(tmp_path: Path) -> None:
    draft = DraftEvent(
        event_type="zeta.turn.completed",
        source="test",
        payload={"turn_id": "turn-1"},
    )
    memory_event = MemoryEventStore().accept(draft).event
    sqlite_event = SqliteEventStore(tmp_path / "zeta.sqlite3").accept(draft).event

    assert memory_event.payload == {"turn_id": "turn-1"}
    assert sqlite_event.payload == {"turn_id": "turn-1"}


def test_event_stores_normalize_payloads_to_json_native(tmp_path: Path) -> None:
    draft = DraftEvent(
        event_type="zeta.turn.completed",
        source="test",
        payload={
            "turn_id": "turn-1",
            "steps": ("model", "tool"),
            "nested": {"values": (1, 2)},
        },
    )

    memory_event = MemoryEventStore().accept(draft).event
    sqlite_store = SqliteEventStore(tmp_path / "zeta.sqlite3")
    sqlite_event = sqlite_store.accept(draft).event
    expected = {
        "turn_id": "turn-1",
        "steps": ["model", "tool"],
        "nested": {"values": [1, 2]},
    }
    direct_event = Event(
        id="direct-event",
        event_type="zeta.turn.completed",
        source="test",
        payload={"steps": ("direct",)},
        idempotency_key=None,
        caused_by=None,
        session_id=None,
        timestamp_ms=1,
    )

    assert memory_event.payload == expected
    assert sqlite_event.payload == expected
    assert MemoryEventStore().append(direct_event).event.payload == {
        "steps": ["direct"]
    }
    assert sqlite_store.append(direct_event).event.payload == {"steps": ["direct"]}
    assert json.loads(json.dumps(memory_event.payload)) == memory_event.payload
    assert json.loads(json.dumps(sqlite_event.payload)) == sqlite_event.payload


def test_sqlite_event_store_rolls_back_event_when_projection_fails(
    tmp_path: Path,
) -> None:
    class FailingProjection:
        def init_schema(self, connection: sqlite3.Connection) -> None:
            connection.execute(
                "CREATE TABLE projection_probe (event_id TEXT NOT NULL) STRICT"
            )

        def clear(self, connection: sqlite3.Connection) -> None:
            connection.execute("DELETE FROM projection_probe")

        def index(self, connection: sqlite3.Connection, event: Event) -> None:
            connection.execute(
                "INSERT INTO projection_probe (event_id) VALUES (?)",
                (event.id,),
            )
            raise RuntimeError("projection failed")

    store = SqliteEventStore(
        tmp_path / "zeta.sqlite3",
        projections=(FailingProjection(),),
    )
    event = Event(
        id="evt_projection_failure",
        event_type="runtime.queue_item.available",
        source="zeta",
        payload={
            "queue_item_id": "qi_projection_failure",
            "event_id": "evt_source",
            "target_agent": "agent",
            "status": "available",
        },
        idempotency_key=None,
        caused_by=None,
        session_id=None,
        timestamp_ms=1,
    )

    with pytest.raises(RuntimeError, match="projection failed"):
        store.append(event)

    assert store.list_events(Filter()) == []
    assert store.connection.execute("SELECT * FROM projection_probe").fetchall() == []


def test_event_stores_reject_non_json_payloads(tmp_path: Path) -> None:
    draft = DraftEvent(
        event_type="zeta.turn.completed",
        source="test",
        payload={"path": tmp_path},
    )

    with pytest.raises(TypeError):
        MemoryEventStore().accept(draft)
    with pytest.raises(TypeError):
        SqliteEventStore(tmp_path / "zeta.sqlite3").accept(draft)


def test_event_stores_share_the_event_store_protocol(tmp_path: Path) -> None:
    assert isinstance(MemoryEventStore(), EventStoreProtocol)
    sqlite_store = SqliteEventStore(tmp_path / "zeta.sqlite3")

    assert isinstance(sqlite_store, EventStoreProtocol)


class RecordingEventSink:
    def __init__(self, path: Path) -> None:
        self.store = SqliteEventStore(path)
        self.drafts: list[DraftEvent] = []

    def accept(self, draft: DraftEvent) -> AppendOutcome:
        self.drafts.append(draft)
        return self.store.accept(draft)


def test_publish_event_uses_configured_event_sink(tmp_path: Path) -> None:
    sink = RecordingEventSink(tmp_path / "zeta.sqlite3")
    outcome = publish_event(
        DraftEvent(
            event_type="test.published",
            source="test",
            payload={"ok": True},
        ),
        sink=sink,
    )

    assert outcome.inserted is True
    assert [draft.event_type for draft in sink.drafts] == ["test.published"]
    assert sink.store.get(outcome.event.id) == outcome.event


def test_publish_event_requires_an_explicit_sink() -> None:
    publish_without_sink = cast(Any, publish_event)
    with pytest.raises(TypeError):
        publish_without_sink(
            DraftEvent(
                event_type="test.default_sink",
                source="test",
                payload={"ok": True},
            )
        )


def test_sqlite_event_store_filters_and_cursors(tmp_path: Path) -> None:
    store = SqliteEventStore(tmp_path / "zeta.sqlite3")
    first = store.accept(
        DraftEvent(
            event_type="zeta.model_call.completed",
            source="zeta",
            payload={"content": "one"},
            session_id="s1",
        )
    ).event
    second = store.accept(
        DraftEvent(
            event_type="zeta.tool_call.completed",
            source="zeta",
            payload={"name": "read"},
            caused_by=first.id,
            session_id="s1",
        )
    ).event
    third = store.accept(
        DraftEvent(
            event_type="zeta.turn.completed",
            source="zeta",
            payload={"turn_id": "turn-1"},
            session_id="s2",
        )
    ).event

    zeta_events = store.list_events(Filter(event_type_prefix="zeta."))
    after_first = store.list_events(Filter(after_cursor=first.cursor))

    assert [event.id for event in zeta_events] == [first.id, second.id, third.id]
    assert store.list_events(Filter(session_id="s1", caused_by=first.id)) == [second]
    assert [event.id for event in after_first] == [
        event.id for event in store.list_events(Filter()) if event.id != first.id
    ]


def test_durable_timeline_projection_prefers_payload_type() -> None:
    event = Event(
        id="evt_model_usage",
        event_type="zeta.model_call.completed",
        source="zeta",
        payload={"_timeline_type": "model_usage", "usage": {"tokens": 1}},
        idempotency_key=None,
        caused_by=None,
        session_id="s1",
        turn_id=None,
        timestamp_ms=1_000_000,
    )

    projected = event_view(event)

    assert projected["type"] == "model_usage"
    assert "_timeline_type" not in projected


def test_durable_timeline_projection_uses_durable_type_without_payload_type() -> None:
    event = Event(
        id="evt_domain",
        event_type="zeta.turn.completed",
        source="zeta",
        payload={"turn_id": "turn-1"},
        idempotency_key=None,
        caused_by=None,
        session_id="s1",
        turn_id=None,
        timestamp_ms=1_000_000,
    )

    projected = event_view(event)

    assert projected["type"] == "turn.completed"


@pytest.mark.parametrize(
    "store_name",
    [
        pytest.param("memory", id="memory"),
        pytest.param("sqlite", id="sqlite"),
    ],
)
def test_event_stores_share_ordering_idempotency_and_filter_semantics(
    store_name: str,
    tmp_path: Path,
) -> None:
    event_store: MemoryEventStore | SqliteEventStore
    if store_name == "sqlite":
        event_store = SqliteEventStore(tmp_path / "zeta.sqlite3")
    else:
        event_store = MemoryEventStore()
    first = event_store.accept(
        DraftEvent(
            event_type="zeta.model_call.completed",
            source="zeta",
            payload={"content": "first"},
            session_id="s1",
            idempotency_key="model:first",
        )
    ).event
    duplicate = event_store.accept(
        DraftEvent(
            event_type="zeta.model_call.completed",
            source="zeta",
            payload={"content": "replayed"},
            session_id="s1",
            idempotency_key="model:first",
        )
    )
    second = event_store.accept(
        DraftEvent(
            event_type="zeta.tool_call.completed",
            source="zeta",
            payload={"name": "read"},
            caused_by=first.id,
            session_id="s1",
        )
    ).event

    assert duplicate.inserted is False
    assert duplicate.event == first
    assert [event.id for event in event_store.list_events(Filter())] == [
        first.id,
        second.id,
    ]
    assert event_store.list_events(Filter(session_id="s1", caused_by=first.id)) == [
        second
    ]
    assert event_store.list_events(Filter(after_cursor=first.cursor)) == [second]
    assert event_store.children(first.id) == [second]
    assert event_store.causal_chain(second.id) == [first, second]


def test_sqlite_event_store_orders_by_append_sequence(tmp_path: Path) -> None:
    store = SqliteEventStore(tmp_path / "zeta.sqlite3")
    first = store.append(
        Event(
            id="z-event",
            event_type="zeta.model_call.completed",
            source="zeta",
            payload={"content": "first"},
            timestamp_ms=2,
            idempotency_key=None,
            caused_by=None,
            session_id=None,
            turn_id=None,
        )
    ).event
    second = store.append(
        Event(
            id="a-event",
            event_type="zeta.model_call.completed",
            source="zeta",
            payload={"content": "second"},
            timestamp_ms=1,
            idempotency_key=None,
            caused_by=None,
            session_id=None,
            turn_id=None,
        )
    ).event

    assert [event.id for event in store.list_events(Filter())] == [
        "z-event",
        "a-event",
    ]
    assert first.cursor is not None
    assert second.cursor is not None
    assert first.cursor < second.cursor
    assert [
        event.id for event in store.list_events(Filter(after_cursor=first.cursor))
    ] == ["a-event"]


def test_sqlite_event_store_traverses_causality(tmp_path: Path) -> None:
    store = SqliteEventStore(tmp_path / "zeta.sqlite3")
    prompt = store.append(
        Event(
            id="prompt-event",
            event_type="zeta.prompt.submitted",
            source="commas",
            payload={"turn_id": "turn-1"},
            turn_id="turn-1",
            idempotency_key=None,
            caused_by=None,
            session_id=None,
            timestamp_ms=1,
        )
    ).event
    model = store.append(
        Event(
            id="model-event",
            event_type="zeta.model_call.completed",
            source="zeta",
            payload={"turn_id": "turn-1"},
            turn_id="turn-1",
            caused_by=prompt.id,
            idempotency_key=None,
            session_id=None,
            timestamp_ms=2,
        )
    ).event
    tool = store.append(
        Event(
            id="tool-event",
            event_type="zeta.tool_call.completed",
            source="zeta",
            payload={"turn_id": "turn-1"},
            turn_id="turn-1",
            caused_by=model.id,
            idempotency_key=None,
            session_id=None,
            timestamp_ms=3,
        )
    ).event
    store.append(
        Event(
            id="turn-event",
            event_type="zeta.turn.completed",
            source="commas",
            payload={"turn_id": "turn-1"},
            turn_id="turn-1",
            caused_by=tool.id,
            idempotency_key=None,
            session_id=None,
            timestamp_ms=4,
        )
    )

    assert store.children(prompt.id) == [model]
    assert [event.id for event in store.causal_chain("turn-event")] == [
        "prompt-event",
        "model-event",
        "tool-event",
        "turn-event",
    ]
    assert [event.id for event in store.events_for_turn("turn-1")] == [
        "prompt-event",
        "model-event",
        "tool-event",
        "turn-event",
    ]


def test_sqlite_event_store_causal_chain_stops_on_cycles(tmp_path: Path) -> None:
    store = SqliteEventStore(tmp_path / "zeta.sqlite3")
    store.append(
        Event(
            id="event-a",
            event_type="cycle.a",
            source="test",
            payload={},
            caused_by="event-b",
            idempotency_key=None,
            session_id=None,
            turn_id=None,
            timestamp_ms=1,
        )
    )
    store.append(
        Event(
            id="event-b",
            event_type="cycle.b",
            source="test",
            payload={},
            caused_by="event-a",
            idempotency_key=None,
            session_id=None,
            turn_id=None,
            timestamp_ms=2,
        )
    )

    assert [event.id for event in store.causal_chain("event-a")] == [
        "event-b",
        "event-a",
    ]


def test_commas_event_query_helpers_use_zeta_event_log() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            prompt = append_event(
                {"type": "zeta.prompt.submitted", "turn_id": "turn-1"}
            )
            model = append_event(
                {
                    "type": "zeta.model_call.completed",
                    "turn_id": "turn-1",
                    "caused_by": prompt.id,
                }
            )

            assert event_children(prompt.id) == [model]
            assert causal_chain(model.id) == [prompt, model]
            assert events_for_turn("turn-1") == [prompt, model]


def test_sqlite_event_store_events_for_turn_uses_turn_id_column(tmp_path: Path) -> None:
    store = SqliteEventStore(tmp_path / "zeta.sqlite3")
    column_match = store.accept(
        DraftEvent(
            event_type="zeta.model_call.completed",
            source="test",
            payload={"turn_id": "payload-turn"},
            turn_id="column-turn",
        )
    ).event
    store.accept(
        DraftEvent(
            event_type="zeta.model_call.completed",
            source="test",
            payload={"turn_id": "payload-turn"},
            turn_id="other-turn",
        )
    )

    assert store.events_for_turn("column-turn") == [column_match]
    assert store.events_for_turn("payload-turn") == []


def test_sqlite_event_store_events_for_run_uses_run_id_column(tmp_path: Path) -> None:
    store = SqliteEventStore(tmp_path / "zeta.sqlite3")
    column_match = store.accept(
        DraftEvent(
            event_type="zeta.model_call.completed",
            source="test",
            payload={"run_id": "payload-run"},
            run_id="column-run",
        )
    ).event
    store.accept(
        DraftEvent(
            event_type="zeta.model_call.completed",
            source="test",
            payload={"run_id": "payload-run"},
            run_id="other-run",
        )
    )

    assert store.events_for_run("column-run") == [column_match]
    assert store.events_for_run("payload-run") == []


def test_sqlite_event_store_has_no_stream_projection_table(tmp_path: Path) -> None:
    store = SqliteEventStore(tmp_path / "zeta.sqlite3")
    table = store.connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("event_streams",),
    ).fetchone()

    assert table is None
    assert not hasattr(store, "append_if_stream_version")
    assert not hasattr(store, "stream_version")


def test_durable_event_constructors_set_turn_id_and_idempotency_keys() -> None:
    prompt = zeta_turn_draft(
        "zeta.prompt.submitted",
        payload={"content": "hello"},
        turn_id="turn-1",
        session_id="s1",
    )
    model = zeta_events.model_call_draft(
        payload={"content": "answer"},
        turn_id="turn-1",
        session_id="s1",
        caused_by="prompt-event",
        event_id="model-event",
    )
    tool = zeta_events.tool_call_draft(
        payload={"name": "read"},
        turn_id="turn-1",
        session_id="s1",
        caused_by="model-event",
        event_id="tool-event",
    )
    completed = zeta_turn_draft(
        "zeta.turn.completed",
        payload={"outcome": "answered"},
        turn_id="turn-1",
        session_id="s1",
        caused_by="tool-event",
    )
    failed = zeta_turn_draft(
        "zeta.turn.failed",
        payload={"outcome": "failed"},
        turn_id="turn-1",
        session_id="s1",
    )
    aborted = zeta_turn_draft(
        "zeta.turn.failed",
        payload={"outcome": "aborted"},
        turn_id="turn-1",
        session_id="s1",
    )

    assert prompt.event_type == "zeta.prompt.submitted"
    assert prompt.source == "zeta"
    assert prompt.turn_id == "turn-1"
    assert prompt.idempotency_key == "zeta.prompt.submitted:turn-1"
    assert model.event_type == "zeta.model_call.completed"
    assert model.source == "zeta"
    assert model.turn_id == "turn-1"
    assert model.caused_by == "prompt-event"
    assert model.idempotency_key == "zeta.model_call.completed:model-event"
    assert tool.event_type == "zeta.tool_call.completed"
    assert tool.idempotency_key == "zeta.tool_call.completed:tool-event"
    assert completed.event_type == "zeta.turn.completed"
    assert completed.idempotency_key == "zeta.turn.completed:turn-1"
    assert failed.event_type == "zeta.turn.failed"
    assert failed.idempotency_key == "zeta.turn.failed:turn-1"
    assert aborted.event_type == "zeta.turn.failed"
    assert aborted.idempotency_key == "zeta.turn.failed:turn-1"


def test_durable_event_constructor_idempotency_deduplicates_replays(
    tmp_path: Path,
) -> None:
    store = SqliteEventStore(tmp_path / "zeta.sqlite3")
    draft = zeta_turn_draft(
        "zeta.turn.completed",
        payload={"outcome": "answered"},
        turn_id="turn-1",
        session_id="s1",
    )

    first = store.accept(draft)
    second = store.accept(draft)

    assert first.inserted is True
    assert second.inserted is False
    assert first.event == second.event
    assert first.event.id.startswith("evt_")


def zeta_turn_draft(
    event_type: str,
    *,
    payload: dict[str, Any],
    turn_id: str | None,
    session_id: str,
    caused_by: str | None = None,
) -> DraftEvent:
    return DraftEvent(
        event_type=event_type,
        source="zeta",
        payload=payload,
        idempotency_key=f"{event_type}:{turn_id}" if turn_id is not None else None,
        caused_by=caused_by,
        session_id=session_id,
        turn_id=turn_id,
    )


def test_sqlite_event_store_accepts_events_without_idempotency_keys(
    tmp_path: Path,
) -> None:
    store = SqliteEventStore(tmp_path / "zeta.sqlite3")
    event = Event.from_draft(
        DraftEvent(
            event_type="commas.command.accepted",
            source="test",
            payload={"command": "ls"},
        )
    )

    inserted = store.append(event)

    assert inserted.inserted is True


def test_commas_events_command_is_removed() -> None:
    result = CliRunner().invoke(cli, ["events", "--help"])

    assert result.exit_code != 0
    assert "No such command" in result.output


def test_zeta_events_causality_subcommands(tmp_path: Path) -> None:
    store = SqliteEventStore(event_store_path(tmp_path / ".zeta"))
    try:
        prompt = store.accept(
            DraftEvent(
                "zeta.prompt.submitted",
                "test",
                {},
                turn_id="turn-1",
            )
        ).event
        model = store.accept(
            DraftEvent(
                "zeta.model_call.completed",
                "test",
                {},
                caused_by=prompt.id,
                turn_id="turn-1",
            )
        ).event
        tool = store.accept(
            DraftEvent(
                "zeta.tool_call.completed",
                "test",
                {},
                caused_by=model.id,
                turn_id="turn-1",
            )
        ).event
        turn_event = store.accept(
            DraftEvent(
                "zeta.turn.completed",
                "test",
                {},
                caused_by=tool.id,
                turn_id="turn-1",
            )
        ).event
    finally:
        store.close()

    common = ["--state-dir", str(tmp_path / ".zeta")]
    trace = CliRunner().invoke(
        zeta_cli, ["events", "trace", *common, turn_event.id, "--json"]
    )
    root = CliRunner().invoke(
        zeta_cli, ["events", "root", *common, turn_event.id, "--json"]
    )
    descendants = CliRunner().invoke(
        zeta_cli, ["events", "descendants", *common, prompt.id, "--json"]
    )
    turn = CliRunner().invoke(zeta_cli, ["events", "turn", *common, "turn-1", "--json"])
    raw = CliRunner().invoke(
        zeta_cli, ["events", "trace", *common, turn_event.id, "--json", "--raw"]
    )

    assert trace.exit_code == 0, trace.output
    assert [event["id"] for event in json.loads(trace.output)] == [
        prompt.id,
        model.id,
        tool.id,
        turn_event.id,
    ]
    assert root.exit_code == 0, root.output
    assert json.loads(root.output)["id"] == prompt.id
    assert descendants.exit_code == 0, descendants.output
    assert [event["id"] for event in json.loads(descendants.output)] == [
        model.id,
        tool.id,
        turn_event.id,
    ]
    assert turn.exit_code == 0, turn.output
    assert [event["id"] for event in json.loads(turn.output)] == [
        prompt.id,
        model.id,
        tool.id,
        turn_event.id,
    ]
    assert raw.exit_code == 0, raw.output
    assert json.loads(raw.output)[1]["caused_by"] == prompt.id


def test_zeta_events_subcommands_raw_requires_json() -> None:
    result = CliRunner().invoke(zeta_cli, ["events", "trace", "event-1", "--raw"])

    assert result.exit_code == 2
    assert "--raw requires --json" in result.output


def test_session_list_includes_last_event_context() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_state_dir = os.environ.get("ZETA_STATE_DIR")
        old_session_id = os.environ.get("COMMAS_SESSION_ID")
        os.environ["ZETA_STATE_DIR"] = tmp
        os.environ["COMMAS_SESSION_ID"] = "alpha"
        alpha_root = Path(tmp) / "sessions" / "alpha"
        beta_root = Path(tmp) / "sessions" / "beta"
        alpha_root.mkdir(parents=True)
        beta_root.mkdir(parents=True)
        (alpha_root / "last-failure.json").write_text("{}", encoding="utf-8")
        try:
            append_event({"type": "old_alpha", "time": 1.0, "cwd": "/old"})
            append_event({"type": "new_alpha", "time": 2.0, "cwd": "/repo"})
            os.environ["COMMAS_SESSION_ID"] = "beta"
            append_event({"type": "beta_event", "time": 3.0, "cwd": "/other"})

            listed = CliRunner().invoke(cli, ["session", "list", "--json"])
            text = CliRunner().invoke(cli, ["session", "list"])
        finally:
            if old_state_dir is None:
                os.environ.pop("ZETA_STATE_DIR", None)
            else:
                os.environ["ZETA_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("COMMAS_SESSION_ID", None)
            else:
                os.environ["COMMAS_SESSION_ID"] = old_session_id

    assert listed.exit_code == 0, listed.output
    sessions = {session["session_id"]: session for session in json.loads(listed.output)}
    assert sessions["alpha"]["last_cwd"] == "/repo"
    assert sessions["alpha"]["last_event_type"] == "new_alpha"
    assert sessions["alpha"]["last_event_time"] == 2.0
    assert sessions["alpha"]["files"] == ["last-failure.json"]
    assert sessions["beta"]["last_cwd"] == "/other"
    assert text.exit_code == 0, text.output
    assert "alpha\t-\t/repo\tnew_alpha\t" in text.output
    assert "beta\t-\t/other\tbeta_event\t" in text.output


def test_session_rename_adds_display_name_to_current_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZETA_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("COMMAS_SESSION_ID", "alpha")
    append_event({"type": "session_event", "time": 1.0, "cwd": "/repo"})

    renamed = CliRunner().invoke(cli, ["session", "rename", "frontend", "work"])
    shown = CliRunner().invoke(cli, ["session", "show", "--json"])
    listed = CliRunner().invoke(cli, ["session", "list", "--json"])
    text = CliRunner().invoke(cli, ["session", "list"])

    assert renamed.exit_code == 0, renamed.output
    assert "renamed session alpha -> frontend work" in renamed.output
    snapshot = json.loads(shown.output)
    assert snapshot["name"] == "frontend work"
    sessions = {session["session_id"]: session for session in json.loads(listed.output)}
    assert sessions["alpha"]["name"] == "frontend work"
    assert "alpha\tfrontend work\t/repo\tsession_event\t" in text.output


def test_session_rename_rejects_blank_name() -> None:
    result = CliRunner().invoke(cli, ["session", "rename", "   "])

    assert result.exit_code == 2
    assert "session name cannot be blank" in result.output


def test_question_workflows_record_glyph_and_local_tools() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_state_dir = os.environ.get("ZETA_STATE_DIR")
        old_session_id = os.environ.get("COMMAS_SESSION_ID")
        os.environ["ZETA_STATE_DIR"] = tmp
        os.environ["COMMAS_SESSION_ID"] = "test"
        try:
            calls = []

            def fake_answer(*args: object, **kwargs: object) -> int:
                calls.append((args, kwargs))
                return 0

            with patch("commas.workflows.ask.step", side_effect=fake_answer):
                assert ask("what is commas?") == 0
            with patch("commas.workflows.ask.step", side_effect=fake_answer):
                assert ask("what is commas?", tools=("read", "grep", "ls")) == 0
            assert len(calls) == 2
            assert calls[0][1]["workflow"] == "ask"
            assert calls[0][1]["system"] == ASK_SYSTEM_PROMPT
            assert (
                "available tools are read, grep, ls, query_log, and web_search only"
                in calls[0][1]["system"]
            )
            assert calls[0][1]["allowed_tools"] == (
                "read",
                "grep",
                "ls",
                "query_log",
                "web_search",
            )
            assert calls[1][1]["allowed_tools"] == ("read", "grep", "ls")
            assert calls[1][1]["prompt"] == "what is commas?"
            assert calls[1][0] == ("what is commas?",)
        finally:
            if old_state_dir is None:
                os.environ.pop("ZETA_STATE_DIR", None)
            else:
                os.environ["ZETA_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("COMMAS_SESSION_ID", None)
            else:
                os.environ["COMMAS_SESSION_ID"] = old_session_id


def test_question_workflow_requests_tool_calls_on_stdout() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            captured_args: list[object] = []

            def fake_answer(*args: object, **kwargs: object) -> int:
                del kwargs
                captured_args.extend(args)
                return 0

            with patch("commas.workflows.ask.step", side_effect=fake_answer):
                assert ask("inspect pyproject") == 0

    assert captured_args == ["inspect pyproject"]


def test_failure_context_prompt_uses_recorded_failure_without_inventing_output() -> (
    None
):
    with tempfile.TemporaryDirectory() as tmp:
        old_state_dir = os.environ.get("ZETA_STATE_DIR")
        old_session_id = os.environ.get("COMMAS_SESSION_ID")
        os.environ["ZETA_STATE_DIR"] = tmp
        os.environ["COMMAS_SESSION_ID"] = "test"
        try:
            record_failure("bad command", 2, "/tmp")
            failure = json.loads(
                (Path(tmp) / "sessions" / "test" / "last-failure.json").read_text(
                    encoding="utf-8"
                )
            )
            prompt = failure_context_prompt(failure)
            assert failure["glyph"] == "failure"
            assert "Failed command: bad command" in prompt
            assert "Working directory: /tmp" in prompt
            assert "Recent stderr: <not captured>" in prompt
            assert "Recent stdout: <not captured>" in prompt
            assert "Do not invent missing stdout or stderr." in prompt
        finally:
            if old_state_dir is None:
                os.environ.pop("ZETA_STATE_DIR", None)
            else:
                os.environ["ZETA_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("COMMAS_SESSION_ID", None)
            else:
                os.environ["COMMAS_SESSION_ID"] = old_session_id


@pytest.mark.parametrize(
    ("command", "status", "stdout_snippet", "stderr_snippet", "expected"),
    [
        (
            "uv run pytest",
            1,
            "tests/test_parser.py::test_parse FAILED",
            "AssertionError: expected command",
            "AssertionError: expected command",
        ),
        (
            "missing-tool --version",
            127,
            "",
            "zsh: command not found: missing-tool",
            "command not found: missing-tool",
        ),
        (
            "git push origin main",
            128,
            "",
            "fatal: Could not read from remote repository.",
            "Could not read from remote repository",
        ),
        (
            "curl https://example.invalid",
            6,
            "",
            "curl: (6) Could not resolve host: example.invalid",
            "Could not resolve host",
        ),
        (
            "touch /root/nope",
            1,
            "",
            "touch: /root/nope: Permission denied",
            "Permission denied",
        ),
    ],
)
def test_failure_context_prompt_covers_common_failure_fixtures(
    command: str,
    status: int,
    stdout_snippet: str,
    stderr_snippet: str,
    expected: str,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            with patch("commas.failure.cwd_context", return_value={"cwd": "/repo"}):
                record_failure(
                    command,
                    status,
                    "/repo",
                    stdout_snippet=stdout_snippet,
                    stderr_snippet=stderr_snippet,
                )
            failure = json.loads(
                (Path(tmp) / "sessions" / "test" / "last-failure.json").read_text(
                    encoding="utf-8"
                )
            )
            prompt = failure_context_prompt(failure)

    assert f"Failed command: {command}" in prompt
    assert f"Exit status: {status}" in prompt
    assert expected in prompt
    assert "Do not invent missing stdout or stderr." in prompt


def test_failure_records_snippets_and_safe_context() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_state_dir = os.environ.get("ZETA_STATE_DIR")
        old_session_id = os.environ.get("COMMAS_SESSION_ID")
        os.environ["ZETA_STATE_DIR"] = tmp
        os.environ["COMMAS_SESSION_ID"] = "test"
        try:
            with patch(
                "commas.failure.cwd_context",
                return_value={
                    "cwd": "/repo",
                    "git_branch": "main",
                    "git_status": [" M file.py"],
                },
            ):
                record_failure(
                    "pytest tests",
                    1,
                    "/repo",
                    stdout_snippet="stdout line",
                    stderr_snippet="stderr line",
                )
            failure = json.loads(
                (Path(tmp) / "sessions" / "test" / "last-failure.json").read_text(
                    encoding="utf-8"
                )
            )
            assert failure["stdout_snippet"] == "stdout line"
            assert failure["stderr_snippet"] == "stderr line"
            assert failure["context"]["git_branch"] == "main"
            assert failure["context"]["git_status"] == [" M file.py"]
        finally:
            if old_state_dir is None:
                os.environ.pop("ZETA_STATE_DIR", None)
            else:
                os.environ["ZETA_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("COMMAS_SESSION_ID", None)
            else:
                os.environ["COMMAS_SESSION_ID"] = old_session_id


def test_failure_snippets_are_redacted_before_storage() -> None:
    assert (
        truncate_snippet("Authorization: Bearer secret-token")
        == "Authorization: Bearer [REDACTED]"
    )
    assert truncate_snippet("API_KEY=abc123") == "API_KEY=[REDACTED]"
    assert truncate_snippet("aws AKIA1234567890ABCDEF") == "aws [REDACTED_AWS_KEY]"


def read_recent_turns(tmp: str) -> list[dict[str, object]]:
    path = Path(tmp) / "sessions" / "test" / "recent-turns.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line:
            rows.append(json.loads(line))
    return rows


def test_record_turn_appends_command_with_glyph() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            record_turn("ls -la", 0, "/repo")

        rows = read_recent_turns(tmp)
        assert len(rows) == 1
        row = rows[0]
        assert row["command"] == "ls -la"
        assert row["status"] == 0
        assert row["turn_cwd"] == "/repo"
        assert row["glyph"] == "turn"


def test_record_turn_trims_buffer_to_last_fifty_entries() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            for index in range(60):
                record_turn(f"cmd-{index}", 0, "/repo")

        rows = read_recent_turns(tmp)
        assert len(rows) == 50
        assert rows[0]["command"] == "cmd-10"
        assert rows[-1]["command"] == "cmd-59"


def test_record_turn_appends_in_place_under_the_buffer_limit() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            record_turn("cmd-1", 0, "/repo")
            path = Path(tmp) / "sessions" / "test" / "recent-turns.jsonl"
            inode = path.stat().st_ino
            record_turn("cmd-2", 0, "/repo")
            assert path.stat().st_ino == inode

        rows = read_recent_turns(tmp)
        assert [row["command"] for row in rows] == ["cmd-1", "cmd-2"]


RECORD_TURNS_SCRIPT = """
import os
import sys
import time
from commas.sessions import record_turn

marker, ready_path, start_path = sys.argv[1:4]
open(ready_path, "w").close()
while not os.path.exists(start_path):
    time.sleep(0.001)
for index in range(10):
    record_turn(f"cmd-{marker}-{index}", 0, "/repo")
"""


def test_record_turn_keeps_all_turns_across_concurrent_processes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"}
        start_gate = Path(tmp) / "start"
        ready_gates = [Path(tmp) / "ready-a", Path(tmp) / "ready-b"]
        procs = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    RECORD_TURNS_SCRIPT,
                    marker,
                    str(ready),
                    str(start_gate),
                ],
                env=env,
            )
            for marker, ready in zip(("a", "b"), ready_gates, strict=True)
        ]
        deadline = time.monotonic() + 30
        while not all(gate.exists() for gate in ready_gates):
            assert time.monotonic() < deadline
            time.sleep(0.001)
        start_gate.touch()
        for proc in procs:
            assert proc.wait(timeout=60) == 0

        rows = read_recent_turns(tmp)
        assert len(rows) == 20


def test_record_turn_skips_empty_command() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            record_turn("", 0, "/repo")
            record_turn("   ", 0, "/repo")
        assert read_recent_turns(tmp) == []


def test_record_turn_skips_leading_whitespace_command() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            record_turn(" curl -H 'Authorization: Bearer secret' x", 0, "/repo")
            record_turn("\tprintenv SECRET", 0, "/repo")
        assert read_recent_turns(tmp) == []


def test_record_turn_skips_comma_and_commas_commands() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            record_turn(", run tests", 0, "/repo")
            record_turn("? what is this", 0, "/repo")
            record_turn("commas ask hello", 0, "/repo")
            record_turn("__commas_precmd", 0, "/repo")
        rows = read_recent_turns(tmp)
        assert len(rows) == 1
        assert rows[0]["command"] == "? what is this"


def test_record_turn_records_unsupported_caret_text() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            record_turn("^^", 0, "/repo")
        rows = read_recent_turns(tmp)
        assert len(rows) == 1
        assert rows[0]["command"] == "^^"


def test_record_turn_fans_out_to_record_failure_on_nonzero_status() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            with patch("commas.failure.cwd_context", return_value={"cwd": "/repo"}):
                record_turn(
                    "pytest tests",
                    1,
                    "/repo",
                    stdout_snippet="captured stdout",
                    stderr_snippet="captured stderr",
                )

        rows = read_recent_turns(tmp)
        assert len(rows) == 1
        assert rows[0]["command"] == "pytest tests"
        assert rows[0]["status"] == 1

        failure_path = Path(tmp) / "sessions" / "test" / "last-failure.json"
        failure = json.loads(failure_path.read_text(encoding="utf-8"))
        assert failure["command"] == "pytest tests"
        assert failure["status"] == 1
        assert failure["stdout_snippet"] == "captured stdout"
        assert failure["stderr_snippet"] == "captured stderr"


def test_record_turn_persists_redacted_snippets_in_recent_turns() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            record_turn(
                "pytest tests",
                1,
                "/repo",
                stdout_snippet="API_KEY=abc123",
                stderr_snippet="Authorization: Bearer secret-token",
            )

        rows = read_recent_turns(tmp)
        assert rows[0]["stdout_snippet"] == "API_KEY=[REDACTED]"
        assert rows[0]["stderr_snippet"] == "Authorization: Bearer [REDACTED]"


def test_recent_turns_context_includes_compact_snippets() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            record_turn(
                "pytest tests",
                1,
                "/repo",
                stdout_snippet="collected 1 item",
                stderr_snippet="AssertionError: expected true",
            )
            context = recent_turns_context()

    assert "pytest tests (exit 1)" in context
    assert "stderr: AssertionError: expected true" in context
    assert "stdout: collected 1 item" in context


def test_record_turn_does_not_record_failure_on_zero_status() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            record_turn("ls", 0, "/repo")

        rows = read_recent_turns(tmp)
        assert len(rows) == 1
        failure_path = Path(tmp) / "sessions" / "test" / "last-failure.json"
        assert not failure_path.exists()


def write_spool(tmp: str, records: list[tuple[str, ...]]) -> Path:
    root = Path(tmp) / "sessions" / "test"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "shell-turns.spool"
    path.write_text(
        "".join("\x1f".join(fields) + "\x1e" for fields in records),
        encoding="utf-8",
    )
    return path


def test_ingest_spooled_turns_records_commands_with_spool_time() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            write_spool(
                tmp,
                [
                    ("1700000000.25", "echo one", "0", "/repo"),
                    ("1700000001.50", "echo two", "1", "/repo/sub"),
                ],
            )
            assert ingest_spooled_turns() == 2

        rows = read_recent_turns(tmp)
        assert [row["command"] for row in rows] == ["echo one", "echo two"]
        assert rows[0]["time"] == 1700000000.25
        assert rows[0]["status"] == 0
        assert rows[0]["turn_cwd"] == "/repo"
        assert rows[1]["time"] == 1700000001.5
        assert rows[1]["status"] == 1
        assert rows[1]["turn_cwd"] == "/repo/sub"


def test_ingest_spooled_turns_removes_the_spool() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            path = write_spool(tmp, [("1700000000.0", "echo hi", "0", "/repo")])
            ingest_spooled_turns()
            assert not path.exists()
            leftovers = list(path.parent.glob("shell-turns.spool*"))
            assert leftovers == []


def test_ingest_spooled_turns_skips_malformed_records() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            write_spool(
                tmp,
                [
                    ("not-enough-fields",),
                    ("1700000000.0", "echo ok", "0", "/repo"),
                    ("1700000001.0", "echo bad-status", "nope", "/repo"),
                ],
            )
            assert ingest_spooled_turns() == 1

        rows = read_recent_turns(tmp)
        assert [row["command"] for row in rows] == ["echo ok"]


def test_ingest_spooled_turns_fans_out_failure_recording() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            write_spool(tmp, [("1700000000.0", "make build", "2", "/repo")])
            ingest_spooled_turns()

        failure_path = Path(tmp) / "sessions" / "test" / "last-failure.json"
        failure = json.loads(failure_path.read_text(encoding="utf-8"))
        assert failure["command"] == "make build"
        assert failure["status"] == 2


def test_ingest_spooled_turns_without_spool_is_noop() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            assert ingest_spooled_turns() == 0
        assert read_recent_turns(tmp) == []


def test_ingest_spooled_turns_recovers_orphaned_claims() -> None:
    # A crash between claim and delete leaves a .ingesting file behind; old
    # orphans are ingested on the next pass instead of leaking turns forever.
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            path = write_spool(tmp, [("1700000000.0", "echo orphan", "0", "/repo")])
            orphan = path.with_name("shell-turns.spool.999.ingesting")
            path.rename(orphan)
            stale = time.time() - 120
            os.utime(orphan, (stale, stale))
            assert ingest_spooled_turns() == 1
            assert not orphan.exists()

        rows = read_recent_turns(tmp)
        assert [row["command"] for row in rows] == ["echo orphan"]


def test_ingest_spooled_turns_leaves_fresh_claims_alone() -> None:
    # A fresh .ingesting file belongs to a live concurrent CLI process.
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            path = write_spool(tmp, [("1700000000.0", "echo live", "0", "/repo")])
            claim = path.with_name("shell-turns.spool.999.ingesting")
            path.rename(claim)
            assert ingest_spooled_turns() == 0
            assert claim.exists()


def test_cli_invocation_ingests_spooled_turns() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            write_spool(tmp, [("1700000000.0", "echo spooled", "0", "/repo")])
            result = CliRunner().invoke(cli, ["status"])
            assert result.exit_code == 0

        rows = read_recent_turns(tmp)
        assert [row["command"] for row in rows] == ["echo spooled"]


def test_record_turn_cli_command_is_not_public_surface() -> None:
    result = CliRunner().invoke(
        cli,
        ["record-turn", "--status", "0", "--cwd", "/repo", "ls -la"],
    )

    assert result.exit_code == 2
    assert "No such command 'record-turn'" in result.output


def test_run_cli_streams_output_and_records_snippets() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            result = CliRunner().invoke(
                cli,
                [
                    "run",
                    "python",
                    "-c",
                    (
                        "import sys; "
                        "print('stdout line'); "
                        "print('stderr line', file=sys.stderr); "
                        "sys.exit(7)"
                    ),
                ],
            )

        assert result.exit_code == 7
        assert result.stdout == "stdout line\n"
        assert result.stderr == "stderr line\n"
        rows = read_recent_turns(tmp)
        command = rows[-1]["command"]
        assert isinstance(command, str)
        assert command.startswith("python -c ")
        assert rows[-1]["status"] == 7
        assert rows[-1]["stdout_snippet"] == "stdout line\n"
        assert rows[-1]["stderr_snippet"] == "stderr line\n"
        failure = json.loads(
            (Path(tmp) / "sessions" / "test" / "last-failure.json").read_text(
                encoding="utf-8"
            )
        )
        assert failure["stdout_snippet"] == "stdout line\n"
        assert failure["stderr_snippet"] == "stderr line\n"


def test_run_cli_shell_mode_captures_raw_command_string() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {
                "COMMAS_RUN_SHELL": "/bin/sh",
                "ZETA_STATE_DIR": tmp,
                "COMMAS_SESSION_ID": "test",
            },
        ):
            result = CliRunner().invoke(
                cli,
                ["run", "--shell", "printf 'stdout line\\n' | cat"],
            )

        assert result.exit_code == EXIT_OK
        assert result.stdout == "stdout line\n"
        rows = read_recent_turns(tmp)
        assert rows[-1]["command"] == "printf 'stdout line\\n' | cat"
        assert rows[-1]["status"] == EXIT_OK
        assert rows[-1]["stdout_snippet"] == "stdout line\n"


def test_run_cli_maps_signal_death_to_shell_exit_code() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            result = CliRunner().invoke(
                cli,
                [
                    "run",
                    "python",
                    "-c",
                    "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
                ],
            )

        assert result.exit_code == EXIT_SIGNAL_BASE + 15
        rows = read_recent_turns(tmp)
        assert rows[-1]["status"] == EXIT_SIGNAL_BASE + 15


class InterruptedProcess:
    """Fake Popen whose first wait raises KeyboardInterrupt, like Ctrl-C."""

    def __init__(self) -> None:
        self.stdout = BytesIO(b"partial output\n")
        self.stderr = BytesIO(b"")
        self.waits = 0

    def wait(self) -> int:
        self.waits += 1
        if self.waits == 1:
            raise KeyboardInterrupt
        return -2


def test_run_cli_records_turn_and_exits_130_on_ctrl_c() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            with patch(
                "commas.cli.run.start_process",
                return_value=InterruptedProcess(),
            ):
                result = CliRunner().invoke(cli, ["run", "sleep", "100"])

        assert result.exit_code == EXIT_INTERRUPTED
        rows = read_recent_turns(tmp)
        assert rows[-1]["command"] == "sleep 100"
        assert rows[-1]["status"] == EXIT_INTERRUPTED
        assert rows[-1]["stdout_snippet"] == "partial output\n"


def test_run_cli_requires_a_command() -> None:
    result = CliRunner().invoke(cli, ["run"])
    assert result.exit_code == EXIT_USAGE
    assert "missing command to run" in result.output


def test_run_cli_records_missing_executable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            result = CliRunner().invoke(cli, ["run", "definitely-not-a-command"])

        assert result.exit_code == EXIT_COMMAND_NOT_FOUND
        assert "missing executable: definitely-not-a-command" in result.stderr
        rows = read_recent_turns(tmp)
        assert rows[-1]["command"] == "definitely-not-a-command"
        assert rows[-1]["status"] == EXIT_COMMAND_NOT_FOUND
        assert "missing executable" in str(rows[-1]["stderr_snippet"])


def test_recent_turns_returns_empty_when_file_missing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            assert recent_turns() == []


def test_recent_turns_returns_last_n_entries_in_order() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            for index in range(15):
                record_turn(f"cmd-{index}", 0, "/repo")
            turns = recent_turns(limit=5)
        assert [turn["command"] for turn in turns] == [
            "cmd-10",
            "cmd-11",
            "cmd-12",
            "cmd-13",
            "cmd-14",
        ]


def test_fresh_ask_prepends_recent_turns_context_to_zeta_prompt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            record_turn("ls -la", 0, "/repo")
            record_turn("pytest tests/test_foo.py", 1, "/repo")
            captured: dict[str, str] = {}

            def fake_answer(objective: str, **kwargs: object) -> int:
                del objective
                captured["prompt"] = str(kwargs["prompt"])
                return 0

            with patch("commas.workflows.ask.step", side_effect=fake_answer):
                assert ask("what should I do next?") == 0

    prompt = captured["prompt"]
    assert "Recent shell activity:" in prompt
    assert "ls -la" in prompt
    assert "pytest tests/test_foo.py" in prompt
    assert "what should I do next?" in prompt


def test_ask_attaches_active_failure_context_for_unrelated_question() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            record_turn(
                "pytest tests/test_foo.py",
                1,
                "/repo",
                stderr_snippet="AssertionError: no",
            )
            captured: dict[str, str] = {}

            def fake_answer(objective: str, **kwargs: object) -> int:
                del objective
                captured["prompt"] = str(kwargs["prompt"])
                return 0

            with patch("commas.workflows.ask.step", side_effect=fake_answer):
                assert ask("what does this repo do") == 0

    prompt = captured["prompt"]
    assert "Last failed command context:" in prompt
    assert "Failed command: pytest tests/test_foo.py" in prompt
    assert "Recent stderr:" in prompt
    assert "AssertionError: no" in prompt


def test_ask_omits_failure_context_after_successful_turn() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            record_turn(
                "pytest tests/test_foo.py",
                1,
                "/repo",
                stderr_snippet="AssertionError: no",
            )
            record_turn("git status --short", 0, "/repo")
            captured: dict[str, str] = {}

            def fake_answer(objective: str, **kwargs: object) -> int:
                del objective
                captured["prompt"] = str(kwargs["prompt"])
                return 0

            with patch("commas.workflows.ask.step", side_effect=fake_answer):
                assert ask("why failed") == 0

    prompt = captured["prompt"]
    assert "Last failed command context:" not in prompt


def test_fresh_ask_only_includes_shell_activity_since_last_response() -> None:
    from commas import zeta_session_for_commas

    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            record_turn("ls -la", 0, "/repo")
            record_durable_timeline_event(
                {"type": "model", "content": "95 files."},
                runtime_context=zeta_session_for_commas(),
            )
            record_turn("git status --short", 0, "/repo")
            captured: dict[str, str] = {}

            def fake_answer(objective: str, **kwargs: object) -> int:
                del objective
                captured["prompt"] = str(kwargs["prompt"])
                return 0

            with patch("commas.workflows.ask.step", side_effect=fake_answer):
                assert ask("and now?") == 0

    prompt = captured["prompt"]
    assert "git status --short" in prompt
    assert "ls -la" not in prompt


def test_fresh_ask_omits_failure_context_already_seen_by_the_model() -> None:
    from commas import zeta_session_for_commas

    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            record_turn(
                "pytest tests/test_foo.py",
                1,
                "/repo",
                stderr_snippet="AssertionError: no",
            )
            record_durable_timeline_event(
                {"type": "model", "content": "The fixture is wrong."},
                runtime_context=zeta_session_for_commas(),
            )
            captured: dict[str, str] = {}

            def fake_answer(objective: str, **kwargs: object) -> int:
                del objective
                captured["prompt"] = str(kwargs["prompt"])
                return 0

            with patch("commas.workflows.ask.step", side_effect=fake_answer):
                assert ask("how do I fix it?") == 0

    prompt = captured["prompt"]
    assert "Last failed command context:" not in prompt
    assert "Recent shell activity" not in prompt
    assert prompt == "how do I fix it?"


def test_fresh_ask_omits_recent_turns_section_when_none_recorded() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            captured: dict[str, str] = {}

            def fake_answer(objective: str, **kwargs: object) -> int:
                del objective
                captured["prompt"] = str(kwargs["prompt"])
                return 0

            with patch("commas.workflows.ask.step", side_effect=fake_answer):
                assert ask("hello") == 0

    prompt = captured["prompt"]
    assert "Recent shell activity" not in prompt
    assert prompt == "hello"


def test_recent_turns_skips_malformed_lines() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            record_turn("ls", 0, "/repo")
            path = Path(tmp) / "sessions" / "test" / "recent-turns.jsonl"
            path.write_text(
                path.read_text(encoding="utf-8") + "not json\n",
                encoding="utf-8",
            )
            turns = recent_turns()
        assert [turn["command"] for turn in turns] == ["ls"]


def test_no_color_disables_tty_color() -> None:
    saved = os.environ.get("NO_COLOR")
    try:
        os.environ.pop("NO_COLOR", None)
        assert should_color(TtyStringIO())
        os.environ["NO_COLOR"] = "1"
        assert not should_color(TtyStringIO())
        assert not should_color(StringIO())
    finally:
        if saved is None:
            os.environ.pop("NO_COLOR", None)
        else:
            os.environ["NO_COLOR"] = saved


def iter_cli_commands(
    group: click.Group,
    context: click.Context,
) -> list[tuple[str, click.Command]]:
    commands = []
    for name in group.list_commands(context):
        command = group.get_command(context, name)
        assert command is not None, name
        commands.append((name, command))
        if isinstance(command, click.Group):
            commands.extend(
                (f"{name} {subname}", subcommand)
                for subname, subcommand in iter_cli_commands(command, context)
            )
    return commands


def test_every_cli_command_and_option_documents_itself() -> None:
    context = click.Context(cli)
    for path, command in iter_cli_commands(cli, context):
        assert command.help or command.short_help, f"{path} has no help text"
        for param in command.params:
            if not isinstance(param, click.Option):
                continue
            assert param.help, f"{path} {param.opts[0]} has no help text"


def test_session_transcript_renders_conversation() -> None:
    from commas import zeta_session_for_commas

    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            runtime_context = zeta_session_for_commas()
            record_durable_timeline_event(
                {"type": "user_message", "content": "what is commas?"},
                runtime_context=runtime_context,
            )
            record_durable_timeline_event(
                {"type": "model", "content": "A shell assistant."},
                runtime_context=runtime_context,
            )

            result = CliRunner().invoke(cli, ["session", "transcript"])

    assert result.exit_code == 0
    assert "what is commas?" in result.output
    assert "A shell assistant." in result.output


def test_session_transcript_limits_and_dumps_json() -> None:
    from commas import zeta_session_for_commas

    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            runtime_context = zeta_session_for_commas()
            record_durable_timeline_event(
                {"type": "user_message", "content": "first"},
                runtime_context=runtime_context,
            )
            record_durable_timeline_event(
                {"type": "model", "content": "second"},
                runtime_context=runtime_context,
            )

            result = CliRunner().invoke(
                cli, ["session", "transcript", "--limit", "1", "--json"]
            )

    assert result.exit_code == 0
    events = json.loads(result.output)
    assert [event["content"] for event in events] == ["second"]


def test_session_transcript_reports_empty_session() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            result = CliRunner().invoke(cli, ["session", "transcript"])

    assert result.exit_code == 0
    assert "no agent turns recorded" in result.output


def test_session_is_a_group_with_show_as_default() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            help_result = CliRunner().invoke(cli, ["session", "--help"])
            bare = CliRunner().invoke(cli, ["session"])
            explicit = CliRunner().invoke(cli, ["session", "show"])

    assert help_result.exit_code == 0
    assert "Commands:" in help_result.output
    for subcommand in ("show", "path", "list", "clear"):
        assert f"\n  {subcommand} " in help_result.output
    assert bare.exit_code == 0
    assert bare.output.startswith("session test")
    assert explicit.exit_code == 0
    assert explicit.output == bare.output


def test_run_cli_passes_trailing_flags_to_the_command() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"ZETA_STATE_DIR": tmp, "COMMAS_SESSION_ID": "test"},
        ):
            result = CliRunner().invoke(cli, ["run", "echo", "hello", "--shell"])

        assert result.exit_code == 0
        assert result.stdout == "hello --shell\n"
        rows = read_recent_turns(tmp)
        assert rows[-1]["command"] == "echo hello --shell"


def test_session_dir_with_traversal_id_stays_inside_state_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMMAS_SESSION_ID", "../../escape")
    sessions_root = (state_dir() / "sessions").resolve()
    assert session_dir().resolve().is_relative_to(sessions_root)


def test_session_dir_with_explicit_traversal_id_stays_inside_state_dir() -> None:
    sessions_root = (state_dir() / "sessions").resolve()
    assert session_dir("../../escape").resolve().is_relative_to(sessions_root)


def test_session_dir_with_explicit_id_ignores_session_dir_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMMAS_SESSION_DIR", "/tmp/elsewhere")
    assert session_dir("other") == state_dir() / "sessions" / "other"


def test_plain_session_id_is_used_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMMAS_SESSION_ID", "ttys003-1234")
    assert session_id() == "ttys003-1234"


def test_path_unsafe_session_id_maps_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMMAS_SESSION_ID", "../../escape")
    first = session_id()
    second = session_id()
    monkeypatch.setenv("COMMAS_SESSION_ID", "../../other")
    other = session_id()
    assert first == second
    assert first != other
    assert "/" not in first
    assert first not in {".", ".."}


def test_session_list_reports_when_no_sessions_exist() -> None:
    text = CliRunner().invoke(cli, ["session", "list"])
    listed = CliRunner().invoke(cli, ["session", "list", "--json"])

    assert text.exit_code == 0
    assert "no sessions recorded" in text.output
    assert listed.exit_code == 0
    assert json.loads(listed.stdout) == []
