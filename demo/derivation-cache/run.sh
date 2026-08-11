#!/usr/bin/env bash
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"
WORK="$DEMO_DIR/work"
export ZETA_STATE_DIR="$WORK/.zeta"
DB="$ZETA_STATE_DIR/zeta.sqlite3"

zeta_cmd() { (cd "$WORK" && uv run --project "$REPO_ROOT" zeta "$@"); }
inspect() {
  uv run --project "$REPO_ROOT" python "$DEMO_DIR/inspect_substrate.py" "$DB" "$@"
}
refcount_fast() {
  sqlite3 -readonly "$DB" \
    "SELECT COUNT(*) FROM refs WHERE name LIKE 'content-transform/retry/%'" \
    2>/dev/null || echo -1
}
publish_doc() {
  local payload
  payload="$(uv run --project "$REPO_ROOT" python -c '
import json, sys
print(json.dumps({"name": sys.argv[1], "text": open(sys.argv[2]).read().strip()}))
' "$1" "$DEMO_DIR/sources/$1.txt")"
  zeta_cmd events publish docs.ingest --payload-json "$payload" --source demo
}
kill_tree() {
  local child
  for child in $(pgrep -P "$1" 2>/dev/null || true); do kill_tree "$child"; done
  kill -9 "$1" 2>/dev/null || true
}

echo "==> Scaffolding a fresh work project"
rm -rf "$WORK"
mkdir -p "$WORK/agents/events"
sed "s|@BASE_DIR@|$WORK|" "$DEMO_DIR/templates/derive.md.tmpl" \
  > "$WORK/agents/derive.md"
cp "$DEMO_DIR/templates/docs.ingest.json" "$WORK/agents/events/docs.ingest.json"
echo "    project: $WORK"

echo
echo "==> Pass 1: ingest two source documents (computes summaries, live model calls)"
publish_doc alpha
publish_doc beta
t0=$SECONDS
zeta_cmd run
echo "    pass 1 wall clock: $((SECONDS - t0))s"

echo
echo "==> Objects after pass 1 (content-addressed: id = BLAKE3 of the content)"
inspect nodes
DOC_ALPHA_1="$(inspect node-id doc/alpha)"
DOC_BETA_1="$(inspect node-id doc/beta)"
CALLS_1="$(inspect modelcalls)"
echo "    summarization model calls so far: $CALLS_1"

echo
echo "==> Derivation graph for summary/alpha (derived object -> model response -> prompt -> source)"
inspect tree summary/alpha

echo
echo "==> Pass 2: republish the SAME two documents as new events"
publish_doc alpha
publish_doc beta
t0=$SECONDS
zeta_cmd run
echo "    pass 2 wall clock: $((SECONDS - t0))s"

DOC_ALPHA_2="$(inspect node-id doc/alpha)"
DOC_BETA_2="$(inspect node-id doc/beta)"
CALLS_2="$(inspect modelcalls)"
echo
echo "==> What pass 2 shows"
echo "    doc/alpha address pass 1: $DOC_ALPHA_1"
echo "    doc/alpha address pass 2: $DOC_ALPHA_2"
if [ "$DOC_ALPHA_1" = "$DOC_ALPHA_2" ] && [ "$DOC_BETA_1" = "$DOC_BETA_2" ]; then
  echo "    IDENTITY: same bytes -> same address; the store kept ONE copy of each source."
else
  echo "    UNEXPECTED: source addresses differ between passes." && exit 1
fi
echo "    summarization model calls now: $CALLS_2 (was $CALLS_1)"
echo "    HONEST NOTE: a republished event is NEW work (a new queue item), so the"
echo "    summaries were recomputed. Zeta's transform cache is scoped to the work"
echo "    item: it prevents re-paying for computation when the SAME work item is"
echo "    retried. Pass 3 demonstrates exactly that."

echo
echo "==> Pass 3: ingest a third document, then kill the worker mid-run"
publish_doc gamma
REFS_BEFORE="$(inspect refcount)"
inspect retryrefs | sort > "$WORK/refs-before-gamma.txt"
(cd "$WORK" && uv run --project "$REPO_ROOT" zeta run) &
WORKER_PID=$!
echo "    worker started (pid $WORKER_PID); waiting for the summary computation to finish..."
deadline=$((SECONDS + 240))
while [ "$(refcount_fast)" -le "$REFS_BEFORE" ]; do
  if [ $SECONDS -ge $deadline ]; then
    echo "    timed out waiting for the transform to complete" && exit 1
  fi
  if ! kill -0 "$WORKER_PID" 2>/dev/null; then
    echo "    worker exited before we could kill it; cannot demonstrate the retry" && exit 1
  fi
  sleep 0.2
done
CALLS_3="$(inspect modelcalls)"
inspect retryrefs | sort > "$WORK/refs-after-gamma.txt"
GAMMA_ASSISTANT_1="$(comm -13 "$WORK/refs-before-gamma.txt" "$WORK/refs-after-gamma.txt" \
  | head -n 1 | awk '{print $3}')"
echo "    summary for gamma is computed and recorded (model calls: $CALLS_3)."
echo "    cached model answer: $GAMMA_ASSISTANT_1"
echo "    KILLING the worker before the attempt can complete."
kill_tree "$WORKER_PID"
wait "$WORKER_PID" 2>/dev/null || true

echo
echo "==> Crash recovery: waiting for the 60s queue lease to expire, then re-running"
sleep 65
t0=$SECONDS
zeta_cmd run
echo "    recovery run wall clock: $((SECONDS - t0))s"

CALLS_4="$(inspect modelcalls)"
GAMMA_ASSISTANT_2="$(inspect summary-links summary/gamma | head -n 1)"
echo
echo "==> What the retry shows"
echo "    summarization model calls before the crash: $CALLS_3"
echo "    summarization model calls after recovery:   $CALLS_4"
echo "    model answer recorded by the killed attempt: $GAMMA_ASSISTANT_1"
echo "    model answer linked by the final summary:    $GAMMA_ASSISTANT_2"
if [ "$CALLS_4" = "$CALLS_3" ] && [ "$GAMMA_ASSISTANT_1" = "$GAMMA_ASSISTANT_2" ]; then
  echo "    REUSE: the retried attempt did NOT call the model again for the summary."
  echo "    It resolved the transform by identity (same work item, same inputs, same"
  echo "    transformation, same model) and reused the recorded answer."
else
  echo "    NO REUSE OBSERVED: the retried attempt recomputed the transform."
  echo "    (The agent must issue a byte-identical transform call for the identity"
  echo "    to match; see README. This run did not reproduce it.)"
  exit 1
fi

echo
echo "==> Derivation graph for summary/gamma (note: its model response was produced by attempt 1)"
inspect tree summary/gamma

echo
echo "==> Attempts for the gamma work item"
echo "    (attempt 1 is the killed one - it never reached a terminal state and"
echo "    still shows as 'running'; attempt 2 completed the same queue item)"
zeta_cmd attempts list | tail -n 6

echo
echo "==> Interpretation"
echo "    Checkpointing frameworks save mutable state and restore it. Zeta gives"
echo "    computation an identity: every artifact is stored at the BLAKE3 address"
echo "    of its content, every derived artifact records what produced it, and a"
echo "    model transform is keyed by (work item, inputs, transformation, model)."
echo "    Identical sources therefore collapse to one stored object across runs,"
echo "    and a crashed-and-retried work item never re-pays for a transform it"
echo "    already computed - the result is found by identity, not by replaying."

echo
echo "==> Inspect the work project yourself (run inside $WORK):"
echo "    export ZETA_STATE_DIR=$ZETA_STATE_DIR"
echo "    uv run --project $REPO_ROOT zeta attempts list"
echo "    uv run --project $REPO_ROOT zeta traces log --all-sessions"
echo "    uv run --project $REPO_ROOT python $DEMO_DIR/inspect_substrate.py $DB retryrefs"
echo "    uv run --project $REPO_ROOT python $DEMO_DIR/inspect_substrate.py $DB tree summary/gamma"
