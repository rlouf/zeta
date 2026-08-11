#!/usr/bin/env bash
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"
PROJECT_DIR="$DEMO_DIR/work/consultancy"
BUNDLE="$DEMO_DIR/work/bundle"

# Pin the runtime state dir so no zeta command can discover a .zeta
# directory further up the tree (zeta walks up from cwd, like git).
export ZETA_STATE_DIR="$PROJECT_DIR/.zeta"

zeta_cmd() {
  (cd "$PROJECT_DIR" && uv run --project "$REPO_ROOT" zeta "$@")
}

# The verifier runs with NO project and NO zeta on its path: stdlib + blake3.
verify_cmd() {
  (cd "$DEMO_DIR/work" && uv run --no-project --with blake3 python "$@")
}

section() {
  printf '\n==> %s\n\n' "$1"
}

section "Scaffolding a fresh zeta project in work/"
rm -rf "$DEMO_DIR/work"
mkdir -p "$DEMO_DIR/work"
(cd "$DEMO_DIR/work" && uv run --project "$REPO_ROOT" zeta new consultancy >/dev/null)
rm -f "$PROJECT_DIR/agents/inbox-summarizer.md"
mkdir -p "$PROJECT_DIR/source" "$PROJECT_DIR/agents/events"
cp "$DEMO_DIR/client-metrics.txt" "$PROJECT_DIR/source/client-metrics.txt"
cp "$DEMO_DIR/report.requested.json" "$PROJECT_DIR/agents/events/report.requested.json"
sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$DEMO_DIR/analyst.md.tmpl" \
  > "$PROJECT_DIR/agents/analyst.md"
echo "project:      $PROJECT_DIR"
echo "agent:        agents/analyst.md (from analyst.md.tmpl)"
echo "seed data:    source/client-metrics.txt"

section "Publishing the trigger event (report.requested)"
zeta_cmd events publish report.requested \
  --payload-json "{\"path\": \"$PROJECT_DIR/source/client-metrics.txt\", \"name\": \"client-metrics.txt\"}" \
  --source client-intake

section "Running the analyst agent (live model calls; about a minute)"
echo "The agent stores the client data verbatim as a content object, derives"
echo "the report through one recorded model transformation, and finishes by"
echo "naming the report's content address."
zeta_cmd run

section "Exporting the provenance bundle (consultant side, uses zeta)"
(cd "$PROJECT_DIR" && uv run --project "$REPO_ROOT" \
  python "$DEMO_DIR/export_bundle.py" "$ZETA_STATE_DIR" "$BUNDLE" client-metrics.txt)
cp "$DEMO_DIR/verify.py" "$BUNDLE/verify.py"
echo
echo "bundle contents:"
(cd "$BUNDLE" && find . -type f | sort | sed 's/^/  /')
echo
echo "the report that ships to the client:"
sed 's/^/  | /' "$BUNDLE/report.md"

if cmp -s "$DEMO_DIR/client-metrics.txt" "$BUNDLE/source/client-metrics.txt"; then
  echo
  echo "(the stored source object matches the seed file byte-for-byte)"
else
  echo
  echo "note: the stored source object differs from the seed file — the agent"
  echo "retyped it into the store. The bundle attests what the run actually"
  echo "used, which is the honest thing to attest."
fi

HEAD_ADDR="$(python3 -c "import json; print(json.load(open('$BUNDLE/manifest.json'))['journal']['head_address'])")"
echo
echo "journal head anchor (this is what you'd also send out-of-band): $HEAD_ADDR"

section "Client side: verifying the bundle offline"
echo "Different cwd, 'uv run --no-project': zeta is not importable, and the"
echo "verifier's only non-stdlib dependency is the blake3 package. The head"
echo "captured above is passed as an external anchor."
echo
verify_cmd "$BUNDLE/verify.py" "$BUNDLE" --expected-head "$HEAD_ADDR"

section "Tamper check 1: one flipped byte in the report"
cp -R "$BUNDLE" "$DEMO_DIR/work/bundle-tampered"
python3 - "$DEMO_DIR/work/bundle-tampered/report.md" <<'EOF'
import sys
path = sys.argv[1]
data = bytearray(open(path, "rb").read())
data[7] ^= 0x20
open(path, "wb").write(bytes(data))
print(f"flipped one bit in byte 7 of {path}")
EOF
echo
if verify_cmd "$DEMO_DIR/work/bundle-tampered/verify.py" "$DEMO_DIR/work/bundle-tampered"; then
  echo "ERROR: verifier accepted a tampered bundle" >&2
  exit 1
else
  echo
  echo "(verifier exited non-zero, as it must)"
fi

section "Tamper check 2: rewriting history in the manifest"
rm -rf "$DEMO_DIR/work/bundle-tampered"
cp -R "$BUNDLE" "$DEMO_DIR/work/bundle-tampered"
python3 - "$DEMO_DIR/work/bundle-tampered/manifest.json" <<'EOF'
import json, sys
path = sys.argv[1]
manifest = json.load(open(path))
finish_id = manifest["deliverable"]["finish_event_id"]
event = next(e for e in manifest["journal"]["events"] if e["id"] == finish_id)
event["payload"]["result"]["object_id"] = "b3:" + "0" * 64
json.dump(manifest, open(path, "w"), indent=2, ensure_ascii=False)
print("edited the chain-committed finish event to claim a different deliverable")
EOF
echo
if verify_cmd "$DEMO_DIR/work/bundle-tampered/verify.py" "$DEMO_DIR/work/bundle-tampered" --expected-head "$HEAD_ADDR"; then
  echo "ERROR: verifier accepted a tampered bundle" >&2
  exit 1
else
  echo
  echo "(verifier exited non-zero, as it must)"
fi

section "What just happened"
cat <<'EOF'
A consulting shop shipped a report WITH its provenance, and the client
checked every claim without trusting — or even having — the shop's
infrastructure:

  report.md            the deliverable, exact bytes
  source/...           the data it was derived from, exact bytes
  manifest.json        every stored object's b3: address, the derivation
                       links between them, the run's event chain, and the
                       chain's head address as an anchor
  verify.py            ~300 lines, stdlib + blake3, no zeta, no network

The verifier recomputes every address from bytes it can see: plain BLAKE3
for file contents, domain-separated BLAKE3 over canonical JSON for objects,
derivations, and the journal chain (the derive-key contexts are frozen in
zeta's substrate/journal specs and printed in the manifest). Flip one bit
in the report and its address no longer matches. Edit one event and the
recomputed chain head no longer matches the anchor. Nothing is taken on
trust: "this report was derived from this data by this recorded model
call, in this run" is checkable arithmetic, air-gapped.

What the anchor adds: the manifest alone proves internal consistency; a
head address the client received through a second channel (an email, an
invoice, a published hash) additionally proves this is the same run the
consultant committed to — a substituted look-alike bundle recomputes to a
different head.
EOF

section "Inspect it yourself"
cat <<EOF
  cat $BUNDLE/manifest.json | python3 -m json.tool | head -60
  (cd $DEMO_DIR/work && uv run --no-project --with blake3 python bundle/verify.py bundle)
  (cd $PROJECT_DIR && ZETA_STATE_DIR=$ZETA_STATE_DIR uv run --project $REPO_ROOT zeta traces log --session agent/analyst --all --limit 30)
  (cd $PROJECT_DIR && ZETA_STATE_DIR=$ZETA_STATE_DIR uv run --project $REPO_ROOT zeta events list)
EOF
