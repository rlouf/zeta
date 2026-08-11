#!/usr/bin/env bash
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"
WORK="$DEMO_DIR/work"

# Zeta discovers runtime state by walking up from cwd (issue #26); pin it so
# nothing here can attach to the repo root's .zeta. Every command below goes
# through zeta_in, which re-pins the state dir to the project it targets.
export ZETA_STATE_DIR="$WORK/prod/.zeta"

CI() { uv run --project "$REPO_ROOT" python "$DEMO_DIR/ci.py" "$@"; }
say() { printf '\n==> %s\n\n' "$1"; }

zeta_in() {
  local project="$1"
  shift
  (cd "$project" && ZETA_STATE_DIR="$project/.zeta" \
    uv run --project "$REPO_ROOT" zeta "$@")
}

scaffold() { # scaffold <project-dir> <agent-template>
  local project="$1" tmpl="$2"
  mkdir -p "$project/agents/events"
  sed "s|@BASE_DIR@|$project|g" "$DEMO_DIR/$tmpl" \
    > "$project/agents/ticket-triage.md"
  cp "$DEMO_DIR/events/"*.json "$project/agents/events/"
}

# Republish the frozen corpus verbatim into a scratch project, run the agent,
# verify every attempt completed, and reduce the published ticket.triaged
# events to a ticket -> priority outcome map.
replay_corpus() { # replay_corpus <project-dir> <outcomes-file>
  local project="$1" out="$2"
  while IFS=$'\t' read -r etype esource ekey epayload; do
    if [ -n "$ekey" ]; then
      zeta_in "$project" events publish "$etype" --source "$esource" \
        --idempotency-key "$ekey" --payload-json "$epayload"
    else
      zeta_in "$project" events publish "$etype" --source "$esource" \
        --payload-json "$epayload"
    fi
  done < <(CI publish-args "$WORK/corpus.json")
  echo
  zeta_in "$project" run
  echo
  # zeta run exits 0 even when an attempt fails (issue #30), so check.
  zeta_in "$project" attempts list --json | CI assert-attempts 4
  echo
  echo "candidate outcomes (from the scratch project's published events):"
  zeta_in "$project" events list --type-prefix ticket.triaged --json \
    > "$project/triaged.events.json"
  CI outcomes "$project/triaged.events.json" "$out"
}

rm -rf "$WORK"
mkdir -p "$WORK"

# --- Phase 1: production ----------------------------------------------------

say "The setup"
cat <<'EOF'
A ticket-triage agent assigns a priority to every incoming support ticket by
applying four keyword rules. It runs in "production" first; then we edit its
definition twice and review each edit the way CI reviews code: replay
yesterday's real trigger events through the candidate definition in a scratch
environment and diff the outcomes against the recorded baseline.
EOF

scaffold "$WORK/prod" triage-baseline.md.tmpl

say "Production: four tickets arrive (live model calls)"
zeta_in "$WORK/prod" events publish ticket.received --source helpdesk \
  --idempotency-key t-101 \
  --payload-json '{"id":"t-101","subject":"Checkout is down for all EU customers"}'
zeta_in "$WORK/prod" events publish ticket.received --source helpdesk \
  --idempotency-key t-102 \
  --payload-json '{"id":"t-102","subject":"Possible security vulnerability in the password reset flow"}'
zeta_in "$WORK/prod" events publish ticket.received --source helpdesk \
  --idempotency-key t-103 \
  --payload-json '{"id":"t-103","subject":"I was charged twice on my last invoice"}'
zeta_in "$WORK/prod" events publish ticket.received --source helpdesk \
  --idempotency-key t-104 \
  --payload-json '{"id":"t-104","subject":"Feature request: dark mode for the dashboard"}'
echo
zeta_in "$WORK/prod" run
echo
zeta_in "$WORK/prod" attempts list --json | CI assert-attempts 4

say "Freezing the corpus: the exact trigger events, read back from the journal"
zeta_in "$WORK/prod" events list --type-prefix ticket.received --json \
  > "$WORK/prod/received.events.json"
CI corpus "$WORK/prod/received.events.json" "$WORK/corpus.json"
echo
echo "saved to work/corpus.json (type, source, idempotency key, full payload)"

say "Recording the baseline: the outcomes production actually produced"
zeta_in "$WORK/prod" events list --type-prefix ticket.triaged --json \
  > "$WORK/prod/triaged.events.json"
CI outcomes "$WORK/prod/triaged.events.json" "$WORK/baseline.json"
CI assert-outcomes "$WORK/baseline.json" \
  '{"t-101":"critical","t-102":"high","t-103":"medium","t-104":"low"}'

# --- Phase 2: CI run A — a cosmetic edit ------------------------------------

say "CI run A: someone rewords the agent's prompt (no intended behavior change)"
echo "The proposed edit to agents/ticket-triage.md:"
echo
diff -u "$DEMO_DIR/triage-baseline.md.tmpl" "$DEMO_DIR/triage-cosmetic.md.tmpl" \
  | tail -n +3 || true

say "CI run A: replaying the corpus through the candidate in a scratch env"
scaffold "$WORK/ci-a" triage-cosmetic.md.tmpl
replay_corpus "$WORK/ci-a" "$WORK/candidate-a.json"

say "CI run A: outcome diff vs baseline"
CI diff "$WORK/baseline.json" "$WORK/candidate-a.json"
CI assert-outcomes "$WORK/candidate-a.json" "$(cat "$WORK/baseline.json")"
echo
echo "The wording changed, the behavior did not. This edit can ship."

# --- Phase 3: CI run B — a real rules change --------------------------------

say "CI run B: someone escalates security tickets to critical (a rules change)"
echo "The proposed edit to agents/ticket-triage.md:"
echo
diff -u "$DEMO_DIR/triage-baseline.md.tmpl" "$DEMO_DIR/triage-escalation.md.tmpl" \
  | tail -n +3 || true

say "CI run B: replaying the corpus through the candidate in a scratch env"
scaffold "$WORK/ci-b" triage-escalation.md.tmpl
replay_corpus "$WORK/ci-b" "$WORK/candidate-b.json"

say "CI run B: outcome diff vs baseline"
CI diff "$WORK/baseline.json" "$WORK/candidate-b.json"
CI assert-outcomes "$WORK/candidate-b.json" \
  '{"t-101":"critical","t-102":"critical","t-103":"medium","t-104":"low"}'

# --- Phase 4: the review artifact -------------------------------------------

say "The review artifact for change B"
CI table "$WORK/corpus.json" "$WORK/baseline.json" "$WORK/candidate-b.json"

say "How to read this"
cat <<'EOF'
- The corpus is real traffic, not a synthetic eval set. Each row's trigger is
  the byte-exact event production received, read back from the durable
  journal with `zeta events list --json` — type, source, payload, and
  idempotency key all intact — and republished verbatim into a scratch
  project with its own ZETA_STATE_DIR. Production state was never touched.
- The outcomes are typed events, not scraped text. The agent publishes
  ticket.triaged, validated by the runtime against
  agents/events/ticket.triaged.json, so "outcome" has a schema and the diff
  compares structured payloads.
- A GREEN diff (run A) is evidence the edit is cosmetic: same corpus, same
  outcomes. A RED diff (run B) is not a test failure — it is the precise
  behavioral consequence of the edit, per real ticket, old -> new. The
  reviewer approves or rejects the behavior change itself, with the diff as
  the artifact.
- In an actual pipeline: freeze yesterday's corpus with
  `zeta events list --type-prefix ticket.received --json` on the production
  state dir, then on every PR that touches agents/ let the CI job scaffold a
  scratch project from the PR branch, export a fresh ZETA_STATE_DIR,
  republish the corpus, `zeta run`, check `zeta attempts list`, and post the
  outcome table as a PR comment. Gate merges on GREEN, or on a human
  approving the RED diff.
EOF

say "Inspect the run yourself"
cat <<EOF
  cd "$WORK/ci-b" && export ZETA_STATE_DIR="$WORK/ci-b/.zeta"
  uv run --project "$REPO_ROOT" zeta events list
  uv run --project "$REPO_ROOT" zeta attempts list
  diff <(jq . "$WORK/baseline.json") <(jq . "$WORK/candidate-b.json")
  diff -u "$WORK/prod/agents/ticket-triage.md" "$WORK/ci-b/agents/ticket-triage.md"
EOF
