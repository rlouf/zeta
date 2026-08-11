"""The CI side of the demo: corpus capture, outcome collection, diffing.

Everything this script consumes comes out of `zeta events list --json`. The
corpus is the exact trigger events production received, and an outcome is the
exact typed event the agent published, so CI needs no bespoke logging in the
agent itself.
"""

import json
import sys


def load(path):
    with open(path) as f:
        return json.load(f)


def dump(obj, path):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")


def cmd_corpus(events_path, out_path):
    """Freeze `ticket.received` journal records into a replayable corpus."""
    events = sorted(load(events_path), key=lambda e: e["cursor"])
    corpus = [
        {
            "type": e["type"],
            "source": e["source"],
            "idempotency_key": e["idempotency_key"],
            "payload": e["payload"],
        }
        for e in events
    ]
    dump(corpus, out_path)
    for entry in corpus:
        print(f"  {entry['payload']['id']}  [{entry['source']}]  "
              f"\"{entry['payload']['subject']}\"")


def cmd_publish_args(corpus_path):
    """Emit one TSV line per corpus event for `zeta events publish`."""
    for entry in load(corpus_path):
        print("\t".join([
            entry["type"],
            entry["source"],
            entry["idempotency_key"] or "",
            json.dumps(entry["payload"]),
        ]))


def cmd_outcomes(events_path, out_path):
    """Reduce `ticket.triaged` journal records to a ticket -> priority map."""
    outcomes = {}
    for e in load(events_path):
        payload = e["payload"]
        previous = outcomes.setdefault(payload["id"], payload["priority"])
        if previous != payload["priority"]:
            sys.exit(f"conflicting outcomes for {payload['id']}: "
                     f"{previous} vs {payload['priority']}")
    dump(outcomes, out_path)
    for tid in sorted(outcomes):
        print(f"  {tid} -> {outcomes[tid]}")


def cmd_assert_attempts(expected):
    """Fail unless exactly EXPECTED attempts completed (zeta run exits 0
    even when attempts fail — issue #30 — so CI must check for itself)."""
    rows = json.load(sys.stdin)
    completed = [r for r in rows if r["status"] == "completed"]
    other = [r for r in rows if r["status"] != "completed"]
    for row in other:
        print(f"  attempt {row['attempt_id']} ({row['target_agent']}): "
              f"{row['status']}")
    if len(completed) != int(expected):
        sys.exit(f"expected {expected} completed attempts, "
                 f"found {len(completed)} (of {len(rows)} total)")
    print(f"  {len(completed)}/{expected} attempts completed")


def cmd_diff(baseline_path, candidate_path):
    baseline, candidate = load(baseline_path), load(candidate_path)
    changed = []
    for tid in sorted(set(baseline) | set(candidate)):
        old = baseline.get(tid, "<missing>")
        new = candidate.get(tid, "<missing>")
        if old != new:
            changed.append(tid)
            print(f"  changed  {tid}  {old} -> {new}")
        else:
            print(f"  same     {tid}  {old}")
    print()
    if changed:
        print(f"verdict: RED ({len(changed)} of {len(baseline)} outcomes changed)")
    else:
        print(f"verdict: GREEN (all {len(baseline)} outcomes identical)")


def cmd_assert_outcomes(outcomes_path, expected_json):
    outcomes, expected = load(outcomes_path), json.loads(expected_json)
    if outcomes != expected:
        sys.exit(f"outcome mismatch:\n  expected {json.dumps(expected, sort_keys=True)}"
                 f"\n  actual   {json.dumps(outcomes, sort_keys=True)}")
    print("  outcomes are exactly as expected")


def cmd_table(corpus_path, baseline_path, candidate_path):
    baseline, candidate = load(baseline_path), load(candidate_path)
    rows = [("ticket", "trigger (corpus event)", "baseline", "candidate", "changed?")]
    for entry in sorted(load(corpus_path), key=lambda e: e["payload"]["id"]):
        tid = entry["payload"]["id"]
        subject = entry["payload"]["subject"]
        if len(subject) > 44:
            subject = subject[:41] + "..."
        old, new = baseline.get(tid, "?"), candidate.get(tid, "?")
        rows.append((tid, subject, old, new,
                     f"{old} -> {new}" if old != new else "-"))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for i, row in enumerate(rows):
        print("  " + "  ".join(cell.ljust(w) for cell, w in zip(row, widths)))
        if i == 0:
            print("  " + "  ".join("-" * w for w in widths))


COMMANDS = {
    "corpus": cmd_corpus,
    "publish-args": cmd_publish_args,
    "outcomes": cmd_outcomes,
    "assert-attempts": cmd_assert_attempts,
    "diff": cmd_diff,
    "assert-outcomes": cmd_assert_outcomes,
    "table": cmd_table,
}

if __name__ == "__main__":
    COMMANDS[sys.argv[1]](*sys.argv[2:])
