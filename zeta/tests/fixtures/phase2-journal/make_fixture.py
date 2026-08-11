"""Generate the Phase 2 golden replay fixture.

Run ONCE against the pre-Phase-2 runtime, then commit the outputs.
The fixture is the phase's verdict: a journal produced by the code
that exists before the storage change, together with a canonical dump
of the projections that journal produces. Regenerating it after the
storage change would make the golden replay tautological, so this
script is provenance, not a maintained tool.

Outputs, beside this script:
- journal.sqlite3   the pre-change database (events + projections)
- events.jsonl      the logical event rows, seq order, canonical JSON
- projections.json  the canonical projection dump (defined ordering)
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from zeta.events import DraftEvent
from zeta.harness.dispatch import QueueingDispatcher
from zeta.harness.routing import AgentDefinition, EventPattern, ExecutableAgent
from zeta.harness.store import RuntimeEventStore
from zeta.journal.sqlite import event_store_path

FIXTURE_DIR = Path(__file__).resolve().parent

# Tables that derive from events. `locks` is live coordination state
# (discarded on rebuild), and `event_projection_versions` is schema
# bookkeeping; neither is part of the logical projection contract.
PROJECTION_TABLES = (
    "queue_items",
    "attempts",
    "attempt_results",
    "scheduled_events",
    "waits",
    "session_mappings",
)

BIG_TEXT = "payload-line-{index:05d} … öß✓\n"


async def drive_runtime(store: RuntimeEventStore) -> None:
    async def summarize(invocation) -> dict[str, object]:
        return {"summarized": invocation.triggering_event.id}

    agents = (
        ExecutableAgent(
            AgentDefinition(
                "inbox-summarizer",
                (EventPattern("file.created"),),
                session="shared",
            ),
            run=summarize,
        ),
    )
    dispatcher = QueueingDispatcher(
        store.journal,
        store,
        executors=agents,
        worker_name="fixture-worker",
    )
    while await dispatcher.run_next() is not None:
        pass


def main() -> None:
    scratch = Path(tempfile.mkdtemp(prefix="phase2-fixture-"))
    db_path = event_store_path(scratch)
    store = RuntimeEventStore.open(db_path)

    store.accept(
        DraftEvent(
            "file.created",
            "filesystem",
            {"path": "/inbox/a.txt", "name": "a.txt", "dir": "/inbox"},
            idempotency_key="file:/inbox/a.txt",
        )
    )
    duplicate = store.accept(
        DraftEvent(
            "file.created",
            "filesystem",
            {"path": "/inbox/a.txt", "name": "a.txt", "dir": "/inbox"},
            idempotency_key="file:/inbox/a.txt",
        )
    )
    assert not duplicate.inserted, "the duplicate accept must dedupe"
    store.accept(
        DraftEvent(
            "file.created",
            "filesystem",
            {"path": "/inbox/bé ✓.md", "name": "bé ✓.md", "dir": "/inbox"},
            idempotency_key="file:/inbox/bé ✓.md",
        )
    )
    asyncio.run(drive_runtime(store))

    requested = store.accept(
        DraftEvent(
            "release.requested",
            "cli",
            {"version": "1.2.3"},
            idempotency_key="release:1.2.3",
            session_id="agent/release",
            run_id="run_fixture_1",
        )
    ).event
    store.accept(
        DraftEvent(
            "release.ready",
            "release-agent",
            {"version": "1.2.3", "notes": "All tests green."},
            idempotency_key="release:1.2.3:ready",
            caused_by=requested.id,
            session_id="agent/release",
            run_id="run_fixture_1",
        )
    )

    big = "".join(BIG_TEXT.format(index=index) for index in range(4000))
    assert len(big.encode()) > 64 * 1024
    store.accept(
        DraftEvent(
            "doc.imported",
            "fixture",
            {"path": "/docs/big.md", "text": big},
            idempotency_key="doc:/docs/big.md",
        )
    )
    store.accept(DraftEvent("orphan.event", "fixture", {"handled": False}))

    store.close()

    dump_events(db_path)
    dump_projections(db_path)
    shutil.copyfile(db_path, FIXTURE_DIR / "journal.sqlite3")
    print("fixture written to", FIXTURE_DIR)


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def dump_events(db_path: Path) -> None:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    rows = connection.execute("SELECT * FROM events ORDER BY seq").fetchall()
    lines = [canonical({key: row[key] for key in row.keys()}) for row in rows]
    (FIXTURE_DIR / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    connection.close()
    print("events:", len(lines))


def dump_projections(db_path: Path) -> None:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    dump: dict[str, list[dict[str, object]]] = {}
    for table in PROJECTION_TABLES:
        rows = connection.execute(f"SELECT * FROM {table}").fetchall()
        records = sorted(
            ({key: row[key] for key in row.keys()} for row in rows),
            key=canonical,
        )
        dump[table] = records
    (FIXTURE_DIR / "projections.json").write_text(
        json.dumps(dump, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    connection.close()
    print("projection tables:", {table: len(rows) for table, rows in dump.items()})


if __name__ == "__main__":
    main()
