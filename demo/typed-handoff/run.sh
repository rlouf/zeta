#!/usr/bin/env bash
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"
PROJ="$DEMO_DIR/work/proj"

# Zeta discovers its state dir by walking up from cwd, which would find the
# repo root's .zeta. Pin the runtime state inside work/ instead.
export ZETA_STATE_DIR="$PROJ/.zeta"

zeta() { uv run --project "$REPO_ROOT" zeta "$@"; }
say() { printf '\n==> %s\n' "$*"; }
events_json() { zeta events list --limit 200 --json; }

say "Scaffolding a fresh project in work/proj"
rm -rf "$DEMO_DIR/work"
mkdir -p "$PROJ/agents/events" "$PROJ/announcements"
cp "$DEMO_DIR/events/"*.json "$PROJ/agents/events/"
cp "$DEMO_DIR/agents/team-a-digest.md" "$PROJ/agents/"
sed "s|@@PROJECT_ROOT@@|$PROJ|" \
  "$DEMO_DIR/agents/team-b-announcer.md.tpl" > "$PROJ/agents/team-b-announcer.md"
cd "$PROJ"
find agents -type f | sort

say "The handoff contract: agents/events/release.summary.ready.json"
cat agents/events/release.summary.ready.json

say "Publishing the kickoff event (this is what Team A's CI would emit)"
KICKOFF=$(zeta events publish release.notes.requested \
  --payload-json '{"version":"1.4.0","changes":["Fixed a deadlock when two workers drained the same queue","Journal replay is about 3x faster","Scheduled events can now be cancelled before they fire"]}' \
  --source team-a-ci --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["event"]["id"])')
echo "kickoff event: $KICKOFF"

say "zeta run: Team A's digest agent runs (live model call)"
zeta run

say "The schema gate in action (from the durable event log)"
events_json | python3 -c '
import json, sys

events = json.load(sys.stdin)
starts = {}
for e in events:
    p = e.get("payload", {})
    if e["type"] == "zeta.tool_call.started" and p.get("name") == "publish_event":
        starts[p["tool_call_id"]] = p.get("input", {})
    if e["type"] == "zeta.tool_call.failed" and p.get("name") == "publish_event":
        attempted = starts.get(p["tool_call_id"], {})
        err = p.get("result", {}).get("error", {})
        print("REJECTED publish_event call:")
        print("  attempted payload:", json.dumps(attempted.get("payload")))
        print("  runtime error:", err.get("code"), "-", err.get("message"))
    if e["type"] == "zeta.tool_call.completed" and p.get("name") == "publish_event":
        print("ACCEPTED publish_event call:")
        print("  handle:", p.get("result", {}).get("handle"))
for e in events:
    if e["type"] == "release.summary.ready" and e["source"].startswith("agent:"):
        print("TYPED EVENT in the log:")
        print("  id:", e["id"], " source:", e["source"], " caused_by:", e["caused_by"])
        print("  payload:", json.dumps(e["payload"]))
'

SUMMARY=$(events_json | python3 -c '
import json, sys
for e in json.load(sys.stdin):
    if e["type"] == "release.summary.ready" and e["source"].startswith("agent:"):
        print(e["id"]); break
')
# zeta run exits 0 even when an attempt failed, so check the outcome itself.
if [[ -z "$SUMMARY" ]]; then
  echo "ERROR: Team A's run produced no release.summary.ready event" >&2
  zeta attempts list >&2 || true
  exit 1
fi
echo "typed handoff event: $SUMMARY"

say "Causality across the team boundary: zeta events chain $SUMMARY"
zeta events chain "$SUMMARY"

say "Known runtime bug: direct consumption of the agent-published event fails"
zeta attempts list || true
events_json | python3 -c '
import json, sys
for e in json.load(sys.stdin):
    if e["type"] == "runtime.attempt.failed":
        print("announcer attempt error:", e["payload"].get("error"))
'
cat <<'NOTE'
An event published by an agent carries the publishing run's run_id, and the
consumer's queue item adopts it (harness/lifecycle.py, ids.run_id_for_attempt),
so Team B's run collides with Team A's content-store ownership. See README
("Two zeta bugs this demo works around"). The typed event itself is valid and
durable; only its direct dispatch is broken today. We bridge by re-publishing
the byte-identical validated payload, keeping causality with --caused-by.
NOTE

say "Bridging the handoff (identical payload, causal link preserved)"
PAYLOAD=$(events_json | python3 -c '
import json, sys
for e in json.load(sys.stdin):
    if e["type"] == "release.summary.ready" and e["source"].startswith("agent:"):
        print(json.dumps(e["payload"])); break
')
BRIDGE=$(zeta events publish release.summary.ready --payload-json "$PAYLOAD" \
  --caused-by "$SUMMARY" --source handoff-bridge --json \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["event"]["id"])')
echo "bridge event: $BRIDGE"

say "zeta run: Team B's announcer runs (live model call)"
zeta run

say "The artifact Team B produced"
if [[ -f announcements/announcement.md ]]; then
  cat announcements/announcement.md
else
  echo "ERROR: announcements/announcement.md was not written" >&2
  exit 1
fi

say "Full causal chain from the kickoff to Team B's completed run"
TEAM_B_DONE=$(events_json | python3 -c '
import json, sys
last = ""
for e in json.load(sys.stdin):
    if (e["type"] == "runtime.attempt.completed"
            and e["payload"].get("target_agent") == "team-b-announcer"):
        last = e["id"]
print(last)
')
if [[ -n "$TEAM_B_DONE" ]]; then
  zeta events chain "$TEAM_B_DONE"
else
  zeta events descendants "$KICKOFF"
fi

say "Authoring-time rejection: a handoff without a schema cannot even be declared"
cp "$DEMO_DIR/agents/rogue-publisher.md.broken" agents/rogue-publisher.md
if OUT=$(zeta run 2>&1); then
  echo "UNEXPECTED: zeta accepted an agent publishing an unregistered event type"
else
  echo "$OUT" | tail -1
fi
rm agents/rogue-publisher.md

say "Honest edge: the CLI ingress does not validate payloads today"
zeta events publish release.summary.ready --payload-json '{"title": 123}' --source rogue-ci
cat <<'NOTE'
That malformed event was ACCEPTED: 'zeta events publish' is raw, unvalidated
ingress into the log. Today the schema gate sits on the agent-side paths: the
publish_event tool (rejects before the event exists, shown above) and the
returns: structured generation. The README says exactly where validation sits.
NOTE

say "What this demonstrated"
cat <<'NOTE'
- The JSON Schema in agents/events/ is the inter-team API. Team A's agent
  could not put a malformed release.summary.ready into the log: the runtime
  bounced the bad payload back to the model with the exact validation error,
  and only the conforming payload became a durable event.
- The typed event carries caused_by provenance, so debugging follows causality
  across the team boundary: one 'zeta events chain' walks from Team A's CI
  kickoff to the file Team B wrote.
- An agent cannot even declare a handoff for an event type that has no
  registered schema: the project fails to load.

Inspect the run yourself (from demo/typed-handoff/work/proj):
  export ZETA_STATE_DIR="$PWD/.zeta"
  uv run --project ../../../ zeta events chain <event-id>
  uv run --project ../../../ zeta events descendants <kickoff-id>
  uv run --project ../../../ zeta events list --limit 200
  uv run --project ../../../ zeta attempts list
NOTE
echo "kickoff: $KICKOFF"
echo "typed handoff: $SUMMARY"
echo "bridge: $BRIDGE"
