#!/usr/bin/env bash
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"
WORK="$DEMO_DIR/work"

# Without this, zeta discovers runtime state by walking up from cwd and could
# find the repo root's .zeta; pinning keeps the demo fully isolated.
export ZETA_STATE_DIR="$WORK/.zeta"

zeta() { (cd "$WORK" && uv run --project "$REPO_ROOT" zeta "$@"); }

section() {
  echo
  echo "==> $1"
  echo
}

rm -rf "$WORK"
mkdir -p "$WORK/agents/events" "$WORK/replays"
for tmpl in sentiment-tagger shadow-evaluator; do
  sed "s|@BASE_DIR@|$WORK|g" "$DEMO_DIR/$tmpl.md.tmpl" > "$WORK/agents/$tmpl.md"
done
cp "$DEMO_DIR/events/"*.json "$WORK/agents/events/"

section "The production agent"
cat "$WORK/agents/sentiment-tagger.md"

section "Publishing three production feedback events"
zeta events publish feedback.received --source prod --payload-json \
  '{"id":"fb-101","text":"Setup took two minutes and the export feature is exactly what we needed."}'
zeta events publish feedback.received --source prod --payload-json \
  '{"id":"fb-102","text":"The app crashes every time I open the settings page. Unusable."}'
zeta events publish feedback.received --source prod --payload-json \
  '{"id":"fb-103","text":"Great support team, but the mobile app is slow and drains my battery."}'

section "zeta run — production traffic (live model calls, recorded as replayable objects)"
zeta run

section "The day's production prompts, stored as addressable objects"
SESSION="$(zeta traces log --all-sessions --kind prompt --limit 1 | head -n 1 | awk '{print $1}')"
echo "recorded session: $SESSION"
echo
zeta traces prompts --session "$SESSION"

PROMPTS="$(zeta traces log --session "$SESSION" --kind prompt \
  | awk '{a[NR]=$1} END{for(i=NR;i>=1;i--) print a[i]}')"

section "Shadow pass: replay every production prompt against the candidate"
echo "(only the built-in codex profile is configured here, so this is a"
echo " same-profile replay: the candidate configuration equals production."
echo " With a candidate profile in ~/.zeta/models.toml, add --model <profile>"
echo " to the replay command below to make this a cross-model shadow eval.)"
echo
MATCHES=0
DIVERGENCES=0
RESULTS=""
for PID in $PROMPTS; do
  echo "--- replaying prompt $PID"
  OUT="$WORK/replays/$PID.txt"
  zeta traces replay "$PID" --session "$SESSION" --diff | tee "$OUT"
  if [ "$(wc -l < "$OUT")" -gt 3 ]; then
    STATUS="diverged"
    DIVERGENCES=$((DIVERGENCES + 1))
  else
    STATUS="match"
    MATCHES=$((MATCHES + 1))
    echo "(no diff lines: candidate answer is byte-identical to production)"
  fi
  echo "result: $STATUS"
  echo
  RESULTS="$RESULTS{\"prompt\":\"$PID\",\"status\":\"$STATUS\"},"
done
RESULTS="[${RESULTS%,}]"
TOTAL=$((MATCHES + DIVERGENCES))
echo "batch summary: $TOTAL prompts, $MATCHES match, $DIVERGENCES diverged"

section "Publishing the batch results for the evaluator agent"
PAYLOAD="{\"date\":\"$(date +%F)\",\"baseline_profile\":\"codex\",\"candidate_profile\":\"codex\",\"prompts_total\":$TOTAL,\"matches\":$MATCHES,\"divergences\":$DIVERGENCES,\"results\":$RESULTS}"
echo "$PAYLOAD"
zeta events publish shadow.eval.requested --source shadow-eval --payload-json "$PAYLOAD"

section "zeta run — the evaluator publishes a typed model.eval.report event"
zeta run

section "The typed report in the durable journal"
zeta events list --type-prefix model.eval.report
REPORT_ID="$(zeta events list --type-prefix model.eval.report | head -n 1 | awk -F'\t' '{print $4}')"
echo
echo "payload (validated by the runtime against agents/events/model.eval.report.json):"
zeta events list --type-prefix model.eval.report --json \
  | uv run --project "$REPO_ROOT" python -c \
    "import json, sys; print(json.dumps(json.load(sys.stdin), indent=2))"

section "Causal chain of the report (root -> report)"
zeta events chain "$REPORT_ID"

section "How to read this"
cat <<'EOF'
- "payload verified" on each replay: the request was rebuilt byte-for-byte
  from the stored component graph — you replayed the real production input,
  not a lossy log line.
- "match" means the candidate configuration gave the same answer on that
  production prompt. "diverged" prints a unified diff of exactly how the
  candidate's answer differs — that diff is the eval finding, per prompt.
- The model.eval.report event is a typed record in the same durable journal
  as the production traffic: the runtime validated the payload against
  agents/events/model.eval.report.json when the evaluator called
  publish_event (invalid payloads are rejected back to the agent). Any
  downstream agent can accept model.eval.report (plain-string accepts) to
  alert on divergences or track match-rate trends over time. Production
  behavior was never touched: replays are recorded as ModelReplay branches
  next to the originals.
- To run this nightly with no external cron, add to the evaluator agent's
  frontmatter:

    schedules:
      - cron: "0 2 * * *"
        timezone: UTC
        catchup: latest

  Zeta then publishes agent.shadow-evaluator.scheduled each night at 02:00;
  a serve-mode worker picks it up, and the evaluator (granted the bash tool)
  runs the same enumerate-replay-report loop this script just performed.
EOF

section "Inspect the run yourself"
cat <<EOF
  cd "$WORK" && export ZETA_STATE_DIR="$WORK/.zeta"
  uv run --project "$REPO_ROOT" zeta traces log --session $SESSION --all
  uv run --project "$REPO_ROOT" zeta traces tree $(echo "$PROMPTS" | head -n 1) --down --depth 1 --session $SESSION
  uv run --project "$REPO_ROOT" zeta events chain $REPORT_ID
  uv run --project "$REPO_ROOT" zeta events list --limit 20
EOF
