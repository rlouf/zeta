#!/usr/bin/env bash
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"
WORK="$DEMO_DIR/work"

# Pin the runtime state dir explicitly: zeta discovers .zeta by walking up
# from cwd, and this demo must never attach to another project's state.
# The exported value is inherited by the worker we kill and by every
# restarted worker, so all of them share the same journal.
export ZETA_STATE_DIR="$WORK/.zeta"

zeta() { uv run --project "$REPO_ROOT" zeta "$@"; }
py() { uv run --project "$REPO_ROOT" python "$@"; }

section() { printf '\n==> %s\n' "$*"; }

queue_count_with_status() {
  # Prints the number of queue items whose status is in the given set, or "?"
  # while the worker holds the write lock.
  zeta queue list --json 2>/dev/null | py -c "
import json, sys
wanted = set(sys.argv[1].split(','))
try:
    rows = json.load(sys.stdin)
except Exception:
    print('?')
else:
    print(sum(1 for row in rows if row['status'] in wanted))
" "$1" 2>/dev/null || echo "?"
}

nonterminal_count() {
  queue_count_with_status "pending,available,claimed"
}

section "Scaffolding a fresh zeta project in work/"
rm -rf "$WORK"
mkdir -p "$WORK/agents/events" "$WORK/notes" "$WORK/summaries" "$WORK/.zeta"
sed "s|@@WORK@@|$WORK|g" "$DEMO_DIR/summarizer.md.tmpl" \
  > "$WORK/agents/summarizer.md"
cp "$DEMO_DIR/note.created.json" "$WORK/agents/events/note.created.json"
cp "$DEMO_DIR"/seed-notes/*.md "$WORK/notes/"
cd "$WORK"
echo "project: $WORK"
echo "agent:   agents/summarizer.md (tools: read, write; session: per-event)"
echo "notes:   $(ls notes | tr '\n' ' ')"

section "Publishing three note.created events, each with an idempotency key"
FIRST_EVT=""
for name in espresso sourdough compost; do
  out="$(zeta events publish note.created \
    --payload-json "{\"path\": \"$WORK/notes/$name.md\", \"name\": \"$name\"}" \
    --source demo \
    --idempotency-key "note:$name")"
  echo "  $out   (key note:$name)"
  if [ -z "$FIRST_EVT" ]; then
    FIRST_EVT="$(echo "$out" | awk '{print $NF}')"
  fi
done

section "Starting the worker in the background, then kill -9 mid-attempt"
set -m
zeta run > "$WORK/worker-1.log" 2>&1 &
WORKER_PID=$!
set +m
echo "worker started (pid $WORKER_PID); polling until a queue item is claimed..."

claimed=""
for _ in $(seq 1 200); do
  claimed="$(queue_count_with_status claimed)"
  if [ "$claimed" != "?" ] && [ "$claimed" -ge 1 ]; then
    break
  fi
  sleep 0.3
done
if [ "$claimed" = "?" ] || [ "$claimed" -lt 1 ]; then
  echo "ERROR: no queue item was claimed; worker log:" >&2
  cat "$WORK/worker-1.log" >&2
  exit 1
fi
echo "a queue item is claimed; its attempt is running (a live model call)"
sleep 3
kill -9 -- "-$WORKER_PID" 2>/dev/null || kill -9 "$WORKER_PID"
wait "$WORKER_PID" 2>/dev/null || true
KILL_AT=$SECONDS
echo "kill -9 sent to the worker's whole process group. No shutdown hook ran."

sleep 1
claimed="$(queue_count_with_status claimed)"
if [ "$claimed" = "?" ] || [ "$claimed" -lt 1 ]; then
  echo "ERROR: the worker finished before the kill landed; re-run the demo." >&2
  exit 1
fi

section "Durable state right after the crash"
echo "--- zeta queue list (one item still 'claimed' by the dead worker):"
zeta queue list
echo
echo "--- zeta attempts list (the killed attempt has no terminal state):"
zeta attempts list
echo
echo "--- zeta ps:"
zeta ps
echo
echo "--- summaries/ so far:"
ls -l summaries 2>/dev/null || true

section "Restarting the worker: recovery IS the normal startup path"
echo "The dead worker's claim carries a 60s lease. Items it never claimed run"
echo "immediately; its in-flight item becomes reclaimable when the lease"
echo "expires. We simply run 'zeta run' again until the queue drains."
remaining="?"
for _ in $(seq 1 40); do
  printf 'zeta run -> '
  zeta run
  remaining="$(nonterminal_count)"
  echo "   $remaining item(s) not yet terminal (t+$((SECONDS - KILL_AT))s after kill)"
  if [ "$remaining" = "0" ]; then
    break
  fi
  sleep 5
done
if [ "$remaining" != "0" ]; then
  echo "ERROR: queue did not drain after restart" >&2
  exit 1
fi

section "Final state: nothing lost, nothing ran twice"
echo "--- zeta queue list (every item completed exactly once):"
zeta queue list
echo
echo "--- zeta attempts list (killed attempt stays visible; retry is attempt 2):"
zeta attempts list
echo
echo "--- summaries/ (exactly one output per note):"
ls -l summaries
count="$(ls summaries | wc -l | tr -d ' ')"
if [ "$count" != "3" ]; then
  echo "ERROR: expected exactly 3 summaries, found $count" >&2
  exit 1
fi
for f in summaries/*; do
  echo "--- $f"
  cat "$f"
  echo
done
SUMS_BEFORE="$(shasum summaries/* | shasum | awk '{print $1}')"

section "Re-publishing an already-processed event with the same idempotency key"
out="$(zeta events publish note.created \
  --payload-json "{\"path\": \"$WORK/notes/espresso.md\", \"name\": \"espresso\"}" \
  --source demo \
  --idempotency-key "note:espresso")"
echo "  $out"
echo "  original publish returned:  $FIRST_EVT"
REPUB_EVT="$(echo "$out" | awk '{print $NF}')"
if [ "$REPUB_EVT" != "$FIRST_EVT" ]; then
  echo "ERROR: journal returned a different event id on re-publish" >&2
  exit 1
fi
echo "  -> same event id came back: the journal deduplicated at ingress."
echo
printf 'zeta run -> '
zeta run
SUMS_AFTER="$(shasum summaries/* | shasum | awk '{print $1}')"
count="$(ls summaries | wc -l | tr -d ' ')"
if [ "$count" != "3" ] || [ "$SUMS_AFTER" != "$SUMS_BEFORE" ]; then
  echo "ERROR: outputs changed after the duplicate publish" >&2
  exit 1
fi
echo "no new queue item, no agent run, summaries byte-identical."

section "What just happened"
cat <<'EOF'
1. Durable claims: kill -9 destroyed the worker process, but the claim, the
   queue items, and the incomplete attempt survived in the journal. Nothing
   in-memory mattered.
2. Crash-only recovery: there is no special recovery mode. Restarting the
   worker re-ran the same startup path: expired claims are released, the
   in-flight item gets attempt #2, untouched items just run.
3. At-least-once with idempotent effects: the killed attempt may have partially
   run, so the item executed again, but every output has a stable identity
   (one summary file per note), so re-execution converged to exactly one
   artifact per event.
4. Journal dedupe: re-publishing with the same idempotency key returned the
   original evt_ id, created no queue item, and triggered no agent run.
EOF

section "Inspect the durable state yourself (from demo/crash-recovery/work)"
cat <<EOF
  uv run --project $REPO_ROOT zeta attempts list
  uv run --project $REPO_ROOT zeta queue list --json
  uv run --project $REPO_ROOT zeta events list
  uv run --project $REPO_ROOT zeta ps
EOF
