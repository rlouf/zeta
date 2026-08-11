#!/usr/bin/env bash
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"
WORK="$DEMO_DIR/work"

export ZETA_STATE_DIR="$WORK/.zeta"
ZETA=(uv run --project "$REPO_ROOT" zeta)

section() {
  echo
  echo "==> $1"
}

section "Scaffolding the fleet project in work/"
rm -rf "$WORK"
mkdir -p "$WORK/agents/events"
cp "$DEMO_DIR"/templates/events/*.json "$WORK/agents/events/"
for tpl in "$DEMO_DIR"/templates/agents/*.md.tpl; do
  name="$(basename "$tpl" .tpl)"
  sed -e "s|@@WORK_DIR@@|$WORK|g" \
      -e "s|@@REPO_ROOT@@|$REPO_ROOT|g" \
      -e "s|@@STATE_DIR@@|$ZETA_STATE_DIR|g" \
      "$tpl" > "$WORK/agents/$name"
done
echo "agents: greeter (healthy), metrics-reporter (will fail), fleet-sre (the meta-agent)"

cd "$WORK"

section "Publishing fleet traffic: one healthy, one malformed, one nobody handles"
echo "-- ops.ping: the greeter accepts this and will run normally"
"${ZETA[@]}" events publish ops.ping \
  --payload-json '{"source_name": "uptime-monitor"}' --source demo
echo "-- ops.metrics.report WITHOUT its rows field: the metrics-reporter's prompt"
echo "   template indexes event.payload.rows[0], so every attempt fails at render"
"${ZETA[@]}" events publish ops.metrics.report \
  --payload-json '{"source": "billing"}' --source demo
echo "-- ops.telemetry.flush: no agent accepts this event type"
"${ZETA[@]}" events publish ops.telemetry.flush \
  --payload-json '{}' --source demo

section "Draining the queue (zeta run; the greeter makes one live model call)"
"${ZETA[@]}" run

section "Raw fleet state, straight from the durable journal"
echo "-- zeta queue list (expect: completed greeter, dead_lettered metrics-reporter,"
echo "   unhandled telemetry event)"
"${ZETA[@]}" queue list
echo
echo "-- zeta attempts list (expect: one completed greeter attempt and TWO failed"
echo "   metrics-reporter attempts: the retry ran and failed again before dead-lettering)"
"${ZETA[@]}" attempts list
echo
echo "-- zeta ps (runs derived from the same journal)"
"${ZETA[@]}" ps
echo
echo "-- queue lifecycle facts in the journal (the states above are projections of these)"
"${ZETA[@]}" events list --type-prefix runtime.queue_item.

section "Asking the Fleet SRE agent for a diagnosis (live model calls)"
"${ZETA[@]}" events publish fleet.check.requested \
  --payload-json '{"reason": "scheduled fleet health check"}' --source demo
"${ZETA[@]}" run

section "The typed incident report, on the record"
REPORT_JSON="$("${ZETA[@]}" events list --type-prefix fleet.incident.report --json)"
REPORT_ID="$(printf '%s' "$REPORT_JSON" | python3 -c 'import json,sys; e=json.load(sys.stdin); print(e[0]["id"] if e else "")')"
if [ -z "$REPORT_ID" ]; then
  echo "ERROR: no fleet.incident.report event was published" >&2
  exit 1
fi
echo "-- fleet.incident.report payload (validated against agents/events/fleet.incident.report.json):"
printf '%s' "$REPORT_JSON" | python3 -c 'import json,sys; e=json.load(sys.stdin)[0]; print(json.dumps({"id": e["id"], "source": e["source"], "payload": e["payload"]}, indent=2))'
echo
echo "-- the diagnosis is itself a run in the journal (zeta ps):"
"${ZETA[@]}" ps
echo
echo "-- causal chain of the report: the health-check request caused the SRE run"
echo "   which caused the report"
"${ZETA[@]}" events chain "$REPORT_ID"

section "What just happened"
cat <<'EOF'
Every queue item, attempt, retry, and event above is a durable state machine in
one SQLite journal. Nothing was instrumented: the failures were not caught by
logging or tracing bolted onto the workers; they ARE the runtime state. The SRE
agent diagnosed the fleet by running the same CLI queries you just saw (via its
bash tool) and filed a schema-validated fleet.incident.report event. The report
event itself entered routing: no agent subscribes to it, so it is parked as an
unhandled queue item -- also on the record. In an in-process framework a dead
worker takes its state with it; here the diagnosis, its evidence, and its own
run are all replayable queries over the same journal.
EOF

section "Inspect it yourself"
cat <<EOF
cd $WORK
export ZETA_STATE_DIR=$ZETA_STATE_DIR
uv run --project $REPO_ROOT zeta queue list
uv run --project $REPO_ROOT zeta attempts list --json
uv run --project $REPO_ROOT zeta events chain $REPORT_ID
uv run --project $REPO_ROOT zeta traces log
EOF
