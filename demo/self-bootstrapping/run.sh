#!/usr/bin/env bash
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"
WORK="$DEMO_DIR/work"
PROJ="$WORK/backoffice"

# Pin the state dir: zeta discovers `.zeta/` by walking up from cwd, and this
# demo must never touch a journal outside work/.
zeta() {
  (cd "$PROJ" && ZETA_STATE_DIR="$PROJ/.zeta" uv run --project "$REPO_ROOT" zeta "$@")
}

section() {
  echo
  echo "==> $1"
}

section "Scaffolding a fresh back-office project in work/"
rm -rf "$WORK"
mkdir -p "$PROJ/agents/events" "$PROJ/flags"
sed "s|__BASE_DIR__|$PROJ|g" "$DEMO_DIR/templates/scaffolder.md" \
  > "$PROJ/agents/scaffolder.md"
cp "$DEMO_DIR"/templates/events/*.json "$PROJ/agents/events/"
echo "project: $PROJ"
echo "The project starts with exactly ONE agent:"
ls "$PROJ/agents/"*.md | sed "s|$PROJ/||"

section "Step 1: Request a NEW standing responsibility — as an ordinary event"
REQ_LINE="$(zeta events publish agent.requested \
  --payload-json '{"slug":"expense-watchdog","name":"Expense Watchdog","watch_event":"expense.report.received","limit_usd":5000}' \
  --source operator)"
echo "$REQ_LINE"
REQ_ID="$(awk '{print $NF}' <<<"$REQ_LINE")"
echo "No config change, no deploy, no restart was performed. A durable event"
echo "now says: 'this system should watch expense reports over 5000 USD'."

section "Step 2: zeta run — the scaffolder agent AUTHORS the new agent"
zeta run

section "Step 3: The new agent exists as a plain Markdown file in agents/"
NEW_AGENT="$PROJ/agents/expense-watchdog.md"
if [[ ! -f "$NEW_AGENT" ]]; then
  echo "FAIL: scaffolder did not create $NEW_AGENT" >&2
  exit 1
fi
ls "$PROJ/agents/"*.md | sed "s|$PROJ/||"
echo
echo "--- agents/expense-watchdog.md (written by the scaffolder agent) ---"
cat "$NEW_AGENT"
echo
echo "--- end of file ---"

section "Step 4: Validate the generated file with zeta's own spec parser"
(cd "$PROJ" && uv run --project "$REPO_ROOT" python - <<'PY'
from zeta.authoring.spec import load_spec

spec = load_spec("agents/expense-watchdog.md")
assert "expense.report.received" in spec.accepts, spec.accepts
assert "expense.flagged" in spec.publishes, spec.publishes
assert spec.tools == ("write",), spec.tools
print(f"parses as a valid agent spec: slug={spec.slug!r} name={spec.name!r}")
print(f"accepts={list(spec.accepts)} publishes={list(spec.publishes)} tools={list(spec.tools)}")
PY
)

CREATED_LINE="$(zeta events list --type-prefix agent.created | awk 'NR==1')"
if [[ "$CREATED_LINE" != *"agent.created"* ]]; then
  echo "FAIL: scaffolder did not publish agent.created" >&2
  exit 1
fi
CREATED_ID="$(awk '{print $NF}' <<<"$CREATED_LINE")"
echo
echo "The creation itself is on the record: agent.created $CREATED_ID"

section "Step 5: Real work for the new agent — an oversized expense report"
EXP_LINE="$(zeta events publish expense.report.received \
  --payload-json '{"report_id":"EXP-2107","employee":"dana@example.com","amount_usd":23400,"description":"Team offsite, chartered boat"}' \
  --source expense-portal \
  --caused-by "$CREATED_ID")"
echo "$EXP_LINE"
echo "(published with --caused-by $CREATED_ID so the audit chain stays connected)"

section "Step 6: zeta run — the MINUTES-OLD agent handles it"
zeta run

section "Step 7: The new agent's output"
FLAG_FILE="$PROJ/flags/EXP-2107.md"
if [[ ! -f "$FLAG_FILE" ]]; then
  echo "FAIL: expense-watchdog did not write $FLAG_FILE" >&2
  exit 1
fi
echo "-- flags/EXP-2107.md (written by expense-watchdog) --"
cat "$FLAG_FILE"
echo
FLAG_LINE="$(zeta events list --type-prefix expense.flagged | awk 'NR==1')"
if [[ "$FLAG_LINE" != *"expense.flagged"* ]]; then
  echo "FAIL: expense-watchdog did not publish expense.flagged" >&2
  exit 1
fi
FLAG_ID="$(awk '{print $NF}' <<<"$FLAG_LINE")"
echo "-- expense.flagged event --"
echo "$FLAG_LINE"

section "Step 8: The record — one causal chain from the flag back to the request"
echo "zeta events chain $FLAG_ID:"
zeta events chain "$FLAG_ID"
echo
echo "-- both runs (creator and created), same process table --"
zeta ps

section "What just happened"
cat <<EOF
1. agent.requested entered the journal like any other event. The scaffolder
   agent handled it with an ordinary write tool call: it AUTHORED
   agents/expense-watchdog.md, then published agent.created. Creating an
   agent is a recorded run with a trace, not a deploy.
2. The next 'zeta run' re-read agents/ and the new agent simply existed.
   There is no registration step, no restart, no redeploy: an agent IS a
   Markdown file, so growing the system is a file write.
3. The new agent then did real work: it judged EXP-2107 (23,400 USD) against
   its 5,000 USD limit, published expense.flagged, and wrote the flag file.
4. The chain above is the whole story in causal order:
   expense.flagged <- expense.report.received <- agent.created <-
   agent.requested. The system's own growth sits in the same audit trail as
   the work it produced.
5. Governance: agents/expense-watchdog.md is reviewable, diffable, and
   revertable like any authored change — delete the file and the
   responsibility is gone; the operator can read exactly what it does (and
   that it only has the 'write' tool) before granting it more.
EOF

section "Inspect it yourself"
echo "cd $PROJ"
echo "export ZETA_STATE_DIR=$PROJ/.zeta"
echo "uv run --project $REPO_ROOT zeta events descendants $REQ_ID"
echo "uv run --project $REPO_ROOT zeta ps --json"
echo "uv run --project $REPO_ROOT zeta traces log"
echo "cat $NEW_AGENT"
