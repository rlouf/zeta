#!/usr/bin/env bash
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"
WORK="$DEMO_DIR/work"
PROJ="$WORK/proj"

# Zeta discovers its state dir by walking up from cwd, which would find the
# repo root's .zeta. Pin the runtime state inside work/ instead.
export ZETA_STATE_DIR="$PROJ/.zeta"

zeta() { uv run --project "$REPO_ROOT" zeta "$@"; }
audit() { uv run --project "$REPO_ROOT" python "$DEMO_DIR/audit.py" "$@"; }
say() { printf '\n==> %s\n' "$*"; }

say "Scaffolding a fresh project in work/proj"
rm -rf "$WORK"
mkdir -p "$WORK"
zeta new "$PROJ"
rm -f "$PROJ"/agents/*.md            # drop the scaffolded inbox summarizer
mkdir -p "$PROJ/agents/events"
cp "$DEMO_DIR/events/"*.json "$PROJ/agents/events/"
cp "$DEMO_DIR/agents/clerk.md" "$PROJ/agents/"
cd "$PROJ"

say "The approvals clerk processes three expense requests (live model calls)"
zeta events publish expense.request --source expense-portal \
  --payload-json '{"request_id":"REQ-1","employee":"maya","description":"Taxi to client site","amount":42.5}'
zeta events publish expense.request --source expense-portal \
  --payload-json '{"request_id":"REQ-2","employee":"jonas","description":"Offsite venue deposit","amount":812.0}'
zeta events publish expense.request --source expense-portal \
  --payload-json '{"request_id":"REQ-3","employee":"priya","description":"External monitor","amount":199.0}'
zeta run

say "Decisions on the record (typed expense.decision events)"
audit decisions "$ZETA_STATE_DIR"

say "Audit step 1: mint the journal-v0 hash chain over the whole journal"
echo "    Every event — the requests, the model calls, the tool calls, the"
echo "    decisions — becomes one chain entry: canonical payload bytes, a"
echo "    BLAKE3 payload address, and an entry address that commits every"
echo "    event field plus the previous entry's address."
mkdir -p "$WORK/audit"
CHAIN="$WORK/audit/chain.json"
audit export "$ZETA_STATE_DIR" "$CHAIN"

say "Audit step 2: unanchored verification (internal consistency only)"
audit verify "$ZETA_STATE_DIR" "$CHAIN"
echo "    Unanchored verification proves the retained sequence is internally"
echo "    consistent. It does NOT prove nothing was removed from the end, and"
echo "    it cannot: a forger who recomputes the whole chain stays consistent."

say "Audit step 3: anchor the head in an external notary (a WORM log stand-in)"
mkdir -p "$WORK/notary"
ANCHOR="$WORK/notary/expense-journal.head"
audit head "$CHAIN" > "$ANCHOR"
echo "    anchored head: $(cat "$ANCHOR")"
echo "    (64 hex chars in an external system are the entire trust root)"
audit verify "$ZETA_STATE_DIR" "$CHAIN" --anchor "$ANCHOR"

say "Attack 1: silently delete the LAST event — in a COPY of the database"
cp -R "$ZETA_STATE_DIR" "$WORK/audit/truncated-state"
audit truncate "$WORK/audit/truncated-state"
echo
echo "--- unanchored verification of the truncated copy ---"
audit verify "$WORK/audit/truncated-state" "$CHAIN"
echo "    Truncation PASSES unanchored verification: the shorter sequence is"
echo "    still a valid chain. This is why the anchor exists."
echo
echo "--- anchored verification of the truncated copy ---"
if audit verify "$WORK/audit/truncated-state" "$CHAIN" --anchor "$ANCHOR"; then
  echo "ERROR: anchored verification unexpectedly passed"; exit 1
fi
echo "    The head no longer matches the notarized address: expected_head."

say "Attack 2: rewrite history — flip one byte in a COPY of the database"
echo "    REQ-2 (\$812.0) was rejected. The tamperer edits the stored request"
echo "    to \$312.0 so the clerk's rejection looks unjustified."
cp -R "$ZETA_STATE_DIR" "$WORK/audit/tampered-state"
audit tamper "$WORK/audit/tampered-state" '812.0' '312.0'
echo
echo "--- verification of the tampered copy ---"
if audit verify "$WORK/audit/tampered-state" "$CHAIN"; then
  echo "ERROR: verification unexpectedly passed"; exit 1
fi
echo "    Verification pinpoints the exact entry: which check failed"
echo "    (payload_encoding: stored payload bytes no longer match the bytes"
echo "    the chain committed to), the event id, its cursor, and how many"
echo "    entries were already verified before the divergence."

say "The real journal is untouched: it still verifies against the anchor"
audit verify "$ZETA_STATE_DIR" "$CHAIN" --anchor "$ANCHOR"

say "What just happened"
cat <<'EOF'
1. The clerk's work — requests in, model calls, tool calls, typed decisions
   out — landed in one append-only journal.
2. The auditor never read logs. They minted the journal-v0 hash chain with
   zeta's own library: each entry's address commits every event field, the
   canonical payload bytes, and the previous entry's address.
3. Unanchored verification proves internal consistency of what is retained.
   Anchoring the single head address in an external system upgrades that to
   tamper evidence: byte flips fail at the exact entry, and tail truncation
   — invisible without the anchor — fails the expected_head check.
4. "Tamper-evident" is claimed only relative to the external anchor. That is
   the spec's own honest claim (spec/journal-v0.md §7), and this demo's.
EOF

say "Inspect further (run inside $PROJ)"
echo "    export ZETA_STATE_DIR=\"$PROJ/.zeta\""
echo "    uv run --project \"$REPO_ROOT\" zeta events list --json | head -50"
echo "    uv run --project \"$REPO_ROOT\" python \"$DEMO_DIR/audit.py\" verify \"$PROJ/.zeta\" \"$CHAIN\" --anchor \"$ANCHOR\""
echo "    python3 -m json.tool \"$CHAIN\" | head -30"
echo "    cat \"$ANCHOR\""
