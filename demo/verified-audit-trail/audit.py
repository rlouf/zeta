"""Auditor tooling: mint, anchor, and verify the journal hash chain.

The runtime journal stores logical events; journal-v0 chain identity is
minted from them with zeta's own library (zeta.journal.types). `export`
plays the role a chain-carrying storage backend will play: it fixes each
event's canonical payload bytes, payload address, predecessor link, and
entry address. `verify` then re-checks the live events against that
committed identity, entry by entry, exactly as spec/journal-v0.md section 7
prescribes.
"""

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from zeta.journal.sqlite import SqliteEventStore, zeta_sqlite_path
from zeta.journal.store import Filter
from zeta.journal.types import (
    JournalEntry,
    VerificationError,
    canonical_payload_bytes,
    journal_entry,
    verify_entries,
)

_UNANCHORED = object()


def load_events(state_dir: Path):
    store = SqliteEventStore(zeta_sqlite_path(state_dir), read_only=True)
    try:
        return store.list_events(Filter())
    finally:
        store.close()


def decisions(state_dir: Path) -> None:
    for event in load_events(state_dir):
        if event.event_type != "expense.decision":
            continue
        payload = event.payload
        print(
            f"  {payload['request_id']}: {payload['decision']:8s} "
            f"- {payload['reason']}  (source={event.source})"
        )


def export_chain(state_dir: Path, chain_file: Path) -> None:
    events = load_events(state_dir)
    records = []
    previous = None
    for event in events:
        entry = journal_entry(event, previous)
        records.append(
            {
                "id": event.id,
                "type": event.event_type,
                "cursor": event.cursor,
                "payload_canonical": entry.payload_bytes.decode("utf-8"),
                "payload_address": entry.payload_address,
                "previous_address": entry.previous_address,
                "entry_address": entry.entry_address,
            }
        )
        previous = entry.entry_address
    chain_file.write_text(json.dumps(records, indent=1))
    histogram = Counter(record["type"] for record in records)
    print(f"chain entries minted: {len(records)}")
    for event_type, count in sorted(histogram.items()):
        print(f"  {count:2d}  {event_type}")
    print(f"head address: {previous}")


def chain_head(chain_file: Path) -> None:
    records = json.loads(chain_file.read_text())
    print(records[-1]["entry_address"] if records else "null")


def verify(state_dir: Path, chain_file: Path, anchor_file: Path | None) -> int:
    events = load_events(state_dir)
    records = json.loads(chain_file.read_text())
    entries = [
        JournalEntry(
            event=event,
            payload_bytes=record["payload_canonical"].encode("utf-8"),
            payload_address=record["payload_address"],
            previous_address=record["previous_address"],
            entry_address=record["entry_address"],
        )
        for event, record in zip(events, records)
    ]
    expected_head = _UNANCHORED
    if anchor_file is not None:
        expected_head = anchor_file.read_text().strip() or None
    try:
        if expected_head is _UNANCHORED:
            report = verify_entries(entries)
        else:
            report = verify_entries(entries, expected_head=expected_head)
    except VerificationError as error:
        print("VERIFICATION FAILED")
        print(f"  reason:          {error.reason.value}")
        print(f"  event id:        {error.event_id}")
        print(f"  cursor:          {error.cursor}")
        print(f"  entries checked: {error.entries_checked} (before the divergence)")
        _explain_divergence(error, events, records)
        return 1
    mode = "anchored" if expected_head is not _UNANCHORED else "unanchored"
    print(f"VERIFIED ({mode}): {report.entries_checked} entries checked")
    print(f"  head address: {report.head_address}")
    if len(events) < len(records):
        print(
            f"  note: the chain export names {len(records)} entries but only "
            f"{len(events)} events remain in this database"
        )
    return 0


def _explain_divergence(error: VerificationError, events, records) -> None:
    index = error.entries_checked
    if error.reason.value != "payload_encoding" or index >= min(
        len(events), len(records)
    ):
        return
    committed = records[index]["payload_canonical"]
    current = canonical_payload_bytes(events[index].payload).decode("utf-8")
    print(f"  committed payload bytes: {committed}")
    print(f"  current payload bytes:   {current}")


def tamper(state_dir: Path, old: str, new: str) -> None:
    connection = sqlite3.connect(str(zeta_sqlite_path(state_dir)))
    try:
        row = connection.execute(
            "SELECT id, payload FROM events "
            "WHERE type = 'expense.request' AND payload LIKE ? LIMIT 1",
            (f"%{old}%",),
        ).fetchone()
        if row is None:
            raise SystemExit(f"no expense.request payload contains {old!r}")
        event_id, payload = row
        connection.execute(
            "UPDATE events SET payload = ? WHERE id = ?",
            (payload.replace(old, new, 1), event_id),
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        print(f"tampered event {event_id}")
        print(f"  before: {payload}")
        print(f"  after:  {payload.replace(old, new, 1)}")
    finally:
        connection.close()


def truncate(state_dir: Path) -> None:
    connection = sqlite3.connect(str(zeta_sqlite_path(state_dir)))
    try:
        row = connection.execute(
            "SELECT id, type FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise SystemExit("journal is empty")
        connection.execute("DELETE FROM events WHERE id = ?", (row[0],))
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        print(f"deleted the final journal event: {row[0]} ({row[1]})")
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("export", "verify"):
        sub = commands.add_parser(name)
        sub.add_argument("state_dir", type=Path)
        sub.add_argument("chain_file", type=Path)
        if name == "verify":
            sub.add_argument("--anchor", type=Path, default=None)
    commands.add_parser("head").add_argument("chain_file", type=Path)
    commands.add_parser("decisions").add_argument("state_dir", type=Path)
    sub = commands.add_parser("tamper")
    sub.add_argument("state_dir", type=Path)
    sub.add_argument("old")
    sub.add_argument("new")
    commands.add_parser("truncate").add_argument("state_dir", type=Path)
    args = parser.parse_args()
    if args.command == "export":
        export_chain(args.state_dir, args.chain_file)
    elif args.command == "verify":
        return verify(args.state_dir, args.chain_file, args.anchor)
    elif args.command == "head":
        chain_head(args.chain_file)
    elif args.command == "decisions":
        decisions(args.state_dir)
    elif args.command == "tamper":
        tamper(args.state_dir, args.old, args.new)
    elif args.command == "truncate":
        truncate(args.state_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
