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
mkdir -p "$WORK/agents/events" "$WORK/decisions"
sed "s|@BASE_DIR@|$WORK|g" \
  "$DEMO_DIR/ticket-classifier.md.tmpl" > "$WORK/agents/ticket-classifier.md"
cp "$DEMO_DIR/ticket.created.json" "$WORK/agents/events/ticket.created.json"

section "The agent under migration"
cat "$WORK/agents/ticket-classifier.md"

section "Publishing three production ticket events"
zeta events publish ticket.created --source demo --payload-json \
  '{"id":"t-1001","subject":"Charged twice this month","body":"My card was charged twice for the Pro plan in August."}'
zeta events publish ticket.created --source demo --payload-json \
  '{"id":"t-1002","subject":"Crash when exporting PDF","body":"The app closes immediately every time I click Export as PDF."}'
zeta events publish ticket.created --source demo --payload-json \
  '{"id":"t-1003","subject":"Dark mode please","body":"Would love a dark theme for late-night work."}'

section "zeta run — the classifier handles all three tickets (live model calls)"
zeta run

section "Routing decisions the agent wrote"
for decision in "$WORK"/decisions/*.txt; do
  printf '%s: ' "$(basename "$decision")"
  cat "$decision"
  echo
done

section "Every prompt sent to the model is a stored, addressable object"
SESSION="$(zeta traces log --all-sessions --kind prompt --limit 1 | head -n 1 | awk '{print $1}')"
echo "recorded session: $SESSION"
echo
zeta traces prompts --session "$SESSION"

section "The recorded prompts, newest first"
zeta traces log --session "$SESSION" --kind prompt
NEWEST="$(zeta traces log --session "$SESSION" --kind prompt | head -n 1 | awk '{print $1}')"
OLDEST="$(zeta traces log --session "$SESSION" --kind prompt | tail -n 1 | awk '{print $1}')"

section "zeta traces show $NEWEST — one prompt, with its derivation context"
zeta traces show "$NEWEST" --session "$SESSION"

section "zeta traces diff $OLDEST $NEWEST --stat — what changed between two prompts"
zeta traces diff "$OLDEST" "$NEWEST" --stat --session "$SESSION"

section "zeta traces replay $NEWEST --diff — the migration check"
echo "(--model <profile> replays against a candidate profile from"
echo " ~/.zeta/models.toml; none is configured here, so the replay uses the"
echo " active built-in codex profile — a same-model replay. With a candidate"
echo " profile configured, add --model <candidate> to this exact command to"
echo " run the cross-model migration check.)"
echo
zeta traces replay "$NEWEST" --session "$SESSION" --diff
echo
echo "(no diff lines after the model line = the replayed answer is identical"
echo " to the recorded production answer; both appear in the tree below)"

section "The replay is itself recorded in the trace graph"
zeta traces tree "$NEWEST" --down --depth 1 --session "$SESSION"

section "How to read this"
cat <<'EOF'
- "payload verified" on the replay: the request rebuilt from the stored
  component graph hashes to exactly what was sent in production. You replayed
  the real workload, not a reconstruction of it.
- An empty answer diff means the candidate model made the same decision on
  this production prompt. A non-empty diff shows, line by line, where the
  candidate diverges — that is the evidence a migration decision looks at,
  prompt by prompt, before any traffic moves.
- The component diff between two prompts shows which parts are shared
  (content-addressed, byte-identical) and which differ, so you know exactly
  what context each replayed decision was made with.
- The ModelReplay branch in the tree means the migration experiment is stored
  next to the production run: the comparison is auditable later.
EOF

section "Inspect the run yourself (from $WORK)"
cat <<EOF
  uv run --project "$REPO_ROOT" zeta traces log --session "$SESSION" --all
  uv run --project "$REPO_ROOT" zeta traces show $OLDEST --session "$SESSION"
  uv run --project "$REPO_ROOT" zeta traces diff $OLDEST $NEWEST --session "$SESSION"
  uv run --project "$REPO_ROOT" zeta traces tools --session "$SESSION"
EOF
