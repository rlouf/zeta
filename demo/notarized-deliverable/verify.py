#!/usr/bin/env python3
"""Offline verifier for a zeta provenance bundle.

Runs with the Python standard library plus the `blake3` package — nothing
else. It never imports zeta, never opens a network connection, and never
reads anything outside the bundle directory. Every rule it applies is
written down in the bundle's manifest ("hashing" section) and frozen by
zeta's substrate-v0 and journal-v0 specifications:

  content address    = "b3:" + BLAKE3(bytes)
  object address     = "b3:" + BLAKE3(canonical_json({kind, schema, data,
                       links}), derive_key_context=object context)
  derivation address = "b3:" + BLAKE3(canonical_json({producer, output_id,
                       input_ids, params}), derive_key_context=derivation
                       context)
  journal entry      = "b3:" + BLAKE3(canonical_json([0, id, type, source,
                       payload_address, idempotency_key, caused_by,
                       session_id, run_id, turn_id, timestamp_ms,
                       previous_address]), derive_key_context=chain context)

Usage: python verify.py <bundle_dir> [--expected-head b3:...]
"""

import argparse
import json
import sys
from pathlib import Path

try:
    from blake3 import blake3
except ImportError:
    sys.exit(
        "this verifier needs the 'blake3' package:\n"
        "    uv run --no-project --with blake3 python verify.py <bundle>"
    )

OBJECT_CONTEXT = "zeta-os 2026-08 cas object"
DERIVATION_CONTEXT = "zeta-os 2026-08 cas derivation"
CHAIN_CONTEXT = "zeta-os 2026-08 cas chain"


def canonical_json_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def content_address(data):
    return "b3:" + blake3(data).hexdigest()


def derived_address(context, data):
    return "b3:" + blake3(data, derive_key_context=context).hexdigest()


class Reporter:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def check(self, ok, label, detail=""):
        if ok:
            self.passed += 1
            print(f"  PASS  {label}")
        else:
            self.failed += 1
            print(f"  FAIL  {label}")
            if detail:
                print(f"        {detail}")
        return ok


def short(address):
    return address[:16] + ".." if isinstance(address, str) else str(address)


def verify_files(bundle, manifest, objects, report):
    print("[1] shipped files against their recorded addresses")
    for entry in manifest["files"]:
        path = bundle / entry["path"]
        if not report.check(path.is_file(), f"{entry['path']} exists"):
            continue
        data = path.read_bytes()
        recomputed = content_address(data)
        report.check(
            recomputed == entry["content_address"],
            f"{entry['path']} bytes hash to {short(entry['content_address'])}",
            f"recomputed {recomputed}, manifest says {entry['content_address']}",
        )
        obj = objects.get(entry["object_id"])
        if not report.check(
            obj is not None,
            f"{entry['path']} names stored object {short(entry['object_id'])}",
        ):
            continue
        report.check(
            obj["data"].get("content") == data.decode("utf-8", errors="replace"),
            f"{entry['path']} bytes equal that object's stored content",
            "the file no longer matches the object it claims to carry",
        )


def verify_objects(report, manifest, objects):
    print("[2] every stored object against its content address")
    mismatched = []
    for obj in manifest["objects"]:
        payload = {
            "kind": obj["kind"],
            "schema": obj["schema"],
            "data": obj["data"],
            "links": obj["links"],
        }
        recomputed = derived_address(OBJECT_CONTEXT, canonical_json_bytes(payload))
        if recomputed != obj["id"]:
            mismatched.append((obj["id"], recomputed))
    report.check(
        not mismatched,
        f"{len(manifest['objects']) - len(mismatched)}/{len(manifest['objects'])}"
        " object addresses recompute from their own bytes",
        "; ".join(f"{oid} recomputes to {new}" for oid, new in mismatched),
    )
    print("[3] link closure")
    dangling = [
        (obj["id"], link)
        for obj in manifest["objects"]
        for link in obj["links"]
        if link not in objects
    ]
    total_links = sum(len(obj["links"]) for obj in manifest["objects"])
    report.check(
        not dangling,
        f"all {total_links} object links resolve inside the bundle",
        "; ".join(f"{short(oid)} links to missing {link}" for oid, link in dangling),
    )


def verify_derivations(report, manifest, objects):
    print("[4] derivation records (who produced what, from what)")
    bad = []
    for derivation in manifest["derivations"]:
        payload = {
            "producer": derivation["producer"],
            "output_id": derivation["output_id"],
            "input_ids": derivation["input_ids"],
            "params": derivation["params"],
        }
        recomputed = derived_address(DERIVATION_CONTEXT, canonical_json_bytes(payload))
        if recomputed != derivation["id"]:
            bad.append(f"{derivation['id']} recomputes to {recomputed}")
        for endpoint in [derivation["output_id"], *derivation["input_ids"]]:
            if endpoint not in objects:
                bad.append(f"{short(derivation['id'])} touches missing {endpoint}")
    report.check(
        not bad,
        f"{len(manifest['derivations'])} derivation addresses recompute;"
        " all endpoints present",
        "; ".join(bad),
    )


def verify_lineage(report, manifest, objects):
    print("[5] the report's recorded lineage")
    deliverable = manifest["deliverable"]
    report_id = deliverable["report_object_id"]
    source_id = deliverable["source_object_id"]
    by_output = {}
    for derivation in manifest["derivations"]:
        by_output.setdefault(derivation["output_id"], []).append(derivation)
    transform = next(
        (
            d
            for d in by_output.get(report_id, [])
            if d["producer"].startswith("ModelTransform")
        ),
        None,
    )
    if not report.check(
        transform is not None,
        f"report {short(report_id)} has a recorded model transformation",
    ):
        return
    report.check(
        source_id in transform["input_ids"],
        f"that transformation reads source {short(source_id)}",
        f"inputs were {transform['input_ids']}",
    )
    response_id = next(
        (
            input_id
            for input_id in transform["input_ids"]
            if objects.get(input_id, {}).get("kind") != "content_node"
        ),
        None,
    )
    if report.check(
        response_id is not None,
        "that transformation names the exact model response it used",
    ):
        response_derivations = by_output.get(response_id, [])
        prompt_ids = [
            input_id for d in response_derivations for input_id in d["input_ids"]
        ]
        report.check(
            bool(prompt_ids),
            f"model response {short(response_id)} links to prompt"
            f" {short(prompt_ids[0]) if prompt_ids else '?'}"
            " (the exact request the model saw)",
        )
        print(
            f"        lineage: report {short(report_id)} <- ModelTransform"
            f" <- source {short(source_id)} + response {short(response_id)}"
        )


def verify_journal(report, manifest, expected_head):
    print("[6] the run's journal hash chain")
    journal = manifest["journal"]
    events = journal["events"]
    previous_address = None
    previous_cursor = None
    seen_ids = set()
    seen_keys = set()
    problem = None
    for event in events:
        cursor = event.get("cursor")
        if not isinstance(cursor, int) or (
            previous_cursor is not None and cursor <= previous_cursor
        ):
            problem = f"cursor order broken at event {event.get('id')}"
            break
        if event["id"] in seen_ids:
            problem = f"duplicate event id {event['id']}"
            break
        key = event.get("idempotency_key")
        if key is not None and key in seen_keys:
            problem = f"duplicate idempotency key {key}"
            break
        payload_address = content_address(canonical_json_bytes(event["payload"]))
        chain_value = [
            0,
            event["id"],
            event["type"],
            event["source"],
            payload_address,
            event.get("idempotency_key"),
            event.get("caused_by"),
            event.get("session_id"),
            event.get("run_id"),
            event.get("turn_id"),
            event["timestamp_ms"],
            previous_address,
        ]
        previous_address = derived_address(
            CHAIN_CONTEXT, canonical_json_bytes(chain_value)
        )
        previous_cursor = cursor
        seen_ids.add(event["id"])
        if key is not None:
            seen_keys.add(key)
    report.check(
        problem is None, f"{len(events)} chained events well-formed", problem or ""
    )
    if problem is None:
        report.check(
            previous_address == journal["head_address"],
            f"recomputed head equals manifest anchor {short(journal['head_address'])}",
            f"recomputed {previous_address}",
        )
        if expected_head is not None:
            report.check(
                previous_address == expected_head,
                "recomputed head equals the externally supplied anchor",
                f"recomputed {previous_address}, expected {expected_head}",
            )
    deliverable = manifest["deliverable"]
    finish = next(
        (e for e in events if e["id"] == deliverable["finish_event_id"]), None
    )
    if report.check(
        finish is not None,
        f"finish event {deliverable['finish_event_id']} is in the chain",
    ):
        committed = (finish["payload"].get("result") or {}).get("object_id")
        report.check(
            committed == deliverable["report_object_id"],
            "the chain-committed finish call names the shipped report object",
            f"chain says {committed}, bundle ships {deliverable['report_object_id']}",
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--expected-head", default=None)
    args = parser.parse_args()

    manifest = json.loads((args.bundle / "manifest.json").read_text())
    objects = {obj["id"]: obj for obj in manifest["objects"]}
    print(f"bundle format: {manifest['format']}")
    print("verifier imports: argparse, json, sys, pathlib, blake3 — no zeta,")
    print("no network, nothing outside the bundle directory\n")

    report = Reporter()
    verify_files(args.bundle, manifest, objects, report)
    verify_objects(report, manifest, objects)
    verify_derivations(report, manifest, objects)
    verify_lineage(report, manifest, objects)
    verify_journal(report, manifest, args.expected_head)

    print(
        f"\nRESULT: {'PASS' if report.failed == 0 else 'FAIL'}"
        f" ({report.passed} checks passed, {report.failed} failed)"
    )
    sys.exit(0 if report.failed == 0 else 1)


if __name__ == "__main__":
    main()
