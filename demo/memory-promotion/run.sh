#!/usr/bin/env bash
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"
WORK="$DEMO_DIR/work"
PROJ="$WORK/project"

zeta() {
  uv run --project "$REPO_ROOT" zeta "$@"
}

echo "==> Scaffolding a fresh zeta project in work/"
rm -rf "$WORK"
mkdir -p "$WORK"
export ZETA_STATE_DIR="$PROJ/.zeta"
zeta new "$PROJ"
rm -f "$PROJ"/agents/*.md            # drop the scaffolded inbox summarizer
mkdir -p "$PROJ/agents/events" "$PROJ/out"
cp "$DEMO_DIR/research.task.json" "$PROJ/agents/events/research.task.json"
sed "s|__BASE_DIR__|$PROJ|g" "$DEMO_DIR/researcher.md.tmpl" \
  > "$PROJ/agents/researcher.md"
cd "$PROJ"
echo "Project ready. Agent: researcher (session: per-event -> each event is a new session)."
echo

echo "==> Run 1 (session A): collect scratch, promote a conclusion"
echo "    The agent stores raw measurements in SESSION scope and its one-line"
echo "    conclusion in AGENT scope, via the transform_content tool."
zeta events publish research.task \
  --source demo \
  --payload-json '{"mode":"collect","scratch":"p99 latency today: checkout-svc 1800ms, cart-svc 210ms, auth-svc 190ms, search-svc 240ms. checkout-svc alert fired twice."}'
zeta run
echo

echo "==> Run 2 (session B): a NEW session reports what memory it can see"
zeta events publish research.task \
  --source demo \
  --payload-json '{"mode":"report"}'
zeta run
echo
echo "--- out/report.txt (written by the session-B agent) ---"
cat "$PROJ/out/report.txt"
echo
echo "-------------------------------------------------------"
echo "The new session sees the promoted conclusion (agent scope) but NOT the"
echo "scratch/measurements key, which stayed in session A's scope."
echo

echo "==> Store inspection: one versioned content head per scope"
echo "    (decoded with zeta's own store API; content heads live in the global"
echo "    scope of .zeta/zeta.sqlite3, next to the event journal)"
uv run --project "$REPO_ROOT" python "$DEMO_DIR/inspect_store.py" "$ZETA_STATE_DIR"
echo

echo "==> Promotion is journaled: runtime.content.promoted events"
zeta events list --type-prefix runtime.content.promoted --json \
  | uv run --project "$REPO_ROOT" python "$DEMO_DIR/show_promotions.py"
echo

echo "==> Stale-overwrite protection (API-level demonstration)"
echo "    Two attempts race to promote a new conclusion through the same"
echo "    ContentWorkspace.promote_all the coordinator uses at attempt success."
uv run --project "$REPO_ROOT" python "$DEMO_DIR/stale_writer.py" "$ZETA_STATE_DIR"
echo

echo "==> What just happened"
cat <<'EOF'
1. Run 1 wrote scratch to session scope and a conclusion to agent scope.
   Neither became durable until the run SUCCEEDED: the coordinator applied the
   promotions inside the attempt-completion transaction and journaled
   runtime.content.promoted.
2. Run 2 started a brand-new session. Its workspace was initialized from the
   durable agent head only, so it saw conclusions/latency but not
   scratch/measurements. Memory is a commit, not a shared blob.
3. Every scope head is a ref that moves by compare-and-swap. A promotion that
   read an outdated head is rejected with ContentConflict, so an old
   computation can never replace newer state.
EOF
echo
echo "==> Inspect further (run inside $PROJ):"
echo "    export ZETA_STATE_DIR=\"$PROJ/.zeta\""
echo "    uv run --project \"$REPO_ROOT\" zeta events list --type-prefix runtime.content.promoted --json"
echo "    uv run --project \"$REPO_ROOT\" zeta attempts list"
echo "    uv run --project \"$REPO_ROOT\" python \"$DEMO_DIR/inspect_store.py\" \"$PROJ/.zeta\""
echo "    sqlite3 \"$PROJ/.zeta/zeta.sqlite3\" 'SELECT scope, name, object_id FROM refs;'"
