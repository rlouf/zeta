"""Export a client-verifiable provenance bundle from a zeta project's store.

This script runs on the consultant's side and may use zeta freely. Nothing
it writes into the bundle requires zeta to check: every address is either a
plain BLAKE3 of exact bytes or a derive-keyed BLAKE3 of canonical JSON, both
frozen by spec/substrate-v0.md and spec/journal-v0.md.

Usage: python export_bundle.py <state_dir> <bundle_dir> <source_file_name>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from zeta.addresses import content_address
from zeta.journal.sqlite import SqliteEventStore, zeta_sqlite_path
from zeta.journal.store import Filter
from zeta.journal.types import journal_entry, verify_entries
from zeta.journal.wire import event_to_wire
from zeta.substrate.sqlite import SqliteObjectStore

BUNDLE_FORMAT = "zeta-provenance-bundle/1"


def find_report_object_id(events: list) -> tuple[str, str]:
    """Return the deliverable's object id and the event that committed it.

    The `finish` tool call's completion event is the run's own durable
    statement of what it delivered; exporting anything else would be
    inventing a claim the journal never made.
    """
    for event in reversed(events):
        if event.event_type != "zeta.tool_call.completed":
            continue
        payload = event.payload
        if payload.get("name") != "finish":
            continue
        result = payload.get("result") or {}
        if result.get("ok") and isinstance(result.get("object_id"), str):
            return result["object_id"], event.id
    raise SystemExit("no successful finish tool call found in the journal")


def main() -> None:
    state_dir = Path(sys.argv[1])
    bundle_dir = Path(sys.argv[2])
    source_name = sys.argv[3]
    db_path = zeta_sqlite_path(state_dir)

    object_store = SqliteObjectStore(db_path, session_id=None, read_only=True)
    event_store = SqliteEventStore(db_path, read_only=True)
    events = event_store.list_events(Filter())

    report_id, finish_event_id = find_report_object_id(events)
    closure = object_store.graph_closure([report_id])

    source_id = next(
        link
        for link in closure[report_id].links
        if closure[link].kind == "content_node"
    )

    derivations = []
    for object_id in closure:
        for derivation in object_store.derivations_for_output(object_id):
            if not set(derivation.input_ids) <= closure.keys():
                continue
            derivations.append(
                {
                    "id": derivation.content_address(),
                    "producer": derivation.producer,
                    "output_id": derivation.output_id,
                    "input_ids": list(derivation.input_ids),
                    "params": derivation.params,
                }
            )

    entries = []
    previous = None
    for event in events:
        entry = journal_entry(event, previous)
        entries.append(entry)
        previous = entry.entry_address
    head = previous
    verify_entries(entries, expected_head=head)

    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "source").mkdir(exist_ok=True)
    report_bytes = closure[report_id].data["content"].encode("utf-8")
    source_bytes = closure[source_id].data["content"].encode("utf-8")
    (bundle_dir / "report.md").write_bytes(report_bytes)
    (bundle_dir / "source" / source_name).write_bytes(source_bytes)

    manifest = {
        "format": BUNDLE_FORMAT,
        "hashing": {
            "algorithm": "BLAKE3-256",
            "address_prefix": "b3:",
            "content": "plain BLAKE3 of exact bytes, no key derivation",
            "object_context": "zeta-os 2026-08 cas object",
            "derivation_context": "zeta-os 2026-08 cas derivation",
            "chain_context": "zeta-os 2026-08 cas chain",
            "canonical_json": (
                "UTF-8 JSON, keys sorted, separators ',' and ':', "
                "non-ASCII unescaped, NaN/Infinity rejected"
            ),
        },
        "deliverable": {
            "report_object_id": report_id,
            "source_object_id": source_id,
            "finish_event_id": finish_event_id,
        },
        "files": [
            {
                "path": "report.md",
                "content_address": content_address(report_bytes),
                "object_id": report_id,
            },
            {
                "path": f"source/{source_name}",
                "content_address": content_address(source_bytes),
                "object_id": source_id,
            },
        ],
        "objects": [
            {
                "id": object_id,
                "kind": obj.kind,
                "schema": obj.schema,
                "data": obj.data,
                "links": list(obj.links),
            }
            for object_id, obj in closure.items()
        ],
        "derivations": derivations,
        "journal": {
            "head_address": head,
            "events": [event_to_wire(event) for event in events],
        },
    }
    manifest_path = bundle_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))

    (bundle_dir / "README.txt").write_text(
        "This bundle carries a client report together with its provenance:\n"
        "the report bytes, the source data it was derived from, every stored\n"
        "object and derivation link between them, and the hash chain of the\n"
        "run that produced it. To verify it offline (no network, no vendor\n"
        "software), you need Python 3.11+ and the 'blake3' package:\n"
        "\n"
        "    uv run --no-project --with blake3 python verify.py .\n"
        "\n"
        "For tamper evidence against substitution of the whole bundle, ask\n"
        "the sender for the journal head address through a second channel\n"
        "and pass it as:  --expected-head b3:...\n"
    )

    print(f"bundle:            {bundle_dir}")
    print(f"report object:     {report_id}")
    print(f"source object:     {source_id}")
    print(f"objects exported:  {len(closure)}")
    print(f"derivations:       {len(derivations)}")
    print(f"journal events:    {len(events)}")
    print(f"journal head:      {head}")
    print(f"finish event:      {finish_event_id}")

    object_store.close()
    event_store.close()


if __name__ == "__main__":
    main()
