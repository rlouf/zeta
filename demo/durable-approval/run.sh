#!/usr/bin/env bash
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"
WORK="$DEMO_DIR/work"
PROJ="$WORK/procurement"

# Pin the state dir: zeta discovers `.zeta/` by walking up from cwd, and this
# demo must never touch a journal outside work/.
zeta() {
  (cd "$PROJ" && ZETA_STATE_DIR="$PROJ/.zeta" uv run --project "$REPO_ROOT" zeta "$@")
}

section() {
  echo
  echo "==> $1"
}

section "Scaffolding a fresh procurement project in work/"
rm -rf "$WORK"
mkdir -p "$PROJ/agents/events"
sed "s|__BASE_DIR__|$PROJ|g" "$DEMO_DIR/templates/procurement.md" \
  > "$PROJ/agents/procurement.md"
cp "$DEMO_DIR"/templates/events/*.json "$PROJ/agents/events/"
echo "project: $PROJ"
echo "agent:   agents/procurement.md (accepts purchase.requested, waits for purchase.approved)"

section "Step 1: An employee files a purchase request (a journal event)"
REQ_LINE="$(zeta events publish purchase.requested \
  --payload-json '{"request_id":"REQ-1001","item":"GPU server","amount_usd":18000}' \
  --source procurement-portal)"
echo "$REQ_LINE"
REQ_ID="$(awk '{print $NF}' <<<"$REQ_LINE")"

section "Step 2: zeta run — the agent files the request and calls wait_for"
zeta run
echo
echo "The zeta process above has already exited. Nothing about this workflow"
echo "lives in any process's memory anymore."

section "Step 3: The wait is durable state (zeta waits list / zeta ps)"
echo "-- waits --"
zeta waits list
echo "-- runs --"
zeta ps
echo "-- processes holding this project's journal open --"
lsof -- "$PROJ/.zeta/zeta.sqlite3" 2>/dev/null \
  || echo "(none — no runtime process is alive; the wait lives only in the journal)"

section "Step 4: Days pass (here: 3 seconds). No daemon is running."
sleep 3

section "Step 5: The CFO approves — the approval itself is a journal event"
APPR_LINE="$(zeta events publish purchase.approved \
  --payload-json '{"request_id":"REQ-1001","approver":"cfo@example.com","note":"Approved within Q3 budget"}' \
  --source approval-portal \
  --caused-by "$REQ_ID")"
echo "$APPR_LINE"
echo "(published with --caused-by $REQ_ID: the approval records what it approves)"

section "Step 6: zeta run again — a brand-new process resumes the parked agent"
zeta run

section "Step 7: The wait is consumed, the run completed"
echo "-- waits --"
zeta waits list
echo "-- runs --"
zeta ps
echo "-- business events in the journal --"
zeta events list --type-prefix purchase.

section "Step 8: Audit — the causal chain from completion back to the request"
COMPLETED_ID="$(zeta events list --type-prefix purchase.completed | awk 'NR==1 {print $NF}')"
echo "chain for $COMPLETED_ID (purchase.completed):"
zeta events chain "$COMPLETED_ID"

section "What just happened"
cat <<'EOF'
1. purchase.requested entered the journal. `zeta run` gave it to the
   procurement agent, which published purchase.filed and called wait_for
   {event_type: purchase.approved, fields: {request_id: REQ-1001}}.
2. The wait was committed to the journal as runtime.wait.created inside the
   same transaction that completed the agent's attempt. The process exited.
   Between steps 3 and 6 there was no zeta process at all.
3. Publishing purchase.approved matched the stored wait in the same
   transaction that stored the event (runtime.wait.matched), and queued a
   continuation for the SAME agent session.
4. A fresh `zeta run` picked up that continuation, and the agent finished by
   publishing purchase.completed.
5. The chain above walks caused_by links: purchase.completed <- the
   continuation <- runtime.wait.matched <- purchase.approved (source:
   approval-portal, approver in the payload) <- purchase.requested. The
   approver is part of the same verifiable record as the agent's own steps.

EOF

section "Inspect it yourself"
echo "cd $PROJ"
echo "export ZETA_STATE_DIR=$PROJ/.zeta"
echo "uv run --project $REPO_ROOT zeta waits list --json"
echo "uv run --project $REPO_ROOT zeta events list"
echo "uv run --project $REPO_ROOT zeta events descendants $REQ_ID"
echo "uv run --project $REPO_ROOT zeta ps --json"
