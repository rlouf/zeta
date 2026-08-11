#!/usr/bin/env bash
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"
PROJECT_DIR="$DEMO_DIR/work/pipeline"
SESSION="agent/note-writer"

# Pin the runtime state dir so no zeta command can discover a .zeta
# directory further up the tree (zeta walks up from cwd, like git).
export ZETA_STATE_DIR="$PROJECT_DIR/.zeta"

zeta_cmd() {
  (cd "$PROJECT_DIR" && uv run --project "$REPO_ROOT" zeta "$@")
}

section() {
  printf '\n==> %s\n\n' "$1"
}

# jq-free field extraction from `zeta traces ... --json` output.
py() { python3 -c "$@"; }

section "Scaffolding a fresh zeta project in work/"
rm -rf "$DEMO_DIR/work"
mkdir -p "$DEMO_DIR/work"
(cd "$DEMO_DIR/work" && uv run --project "$REPO_ROOT" zeta new pipeline >/dev/null)
rm -f "$PROJECT_DIR/agents/inbox-summarizer.md"
mkdir -p "$PROJECT_DIR/source" "$PROJECT_DIR/agents/events"
cp "$DEMO_DIR/transcript.txt" "$PROJECT_DIR/source/transcript.txt"
cp "$DEMO_DIR/transcript.ready.json" "$PROJECT_DIR/agents/events/transcript.ready.json"
sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$DEMO_DIR/note-writer.md.tmpl" \
  > "$PROJECT_DIR/agents/note-writer.md"
echo "project:      $PROJECT_DIR"
echo "agent:        agents/note-writer.md (from note-writer.md.tmpl)"
echo "event schema: agents/events/transcript.ready.json"
echo "transcript:   source/transcript.txt"

section "Publishing the trigger event (transcript.ready)"
zeta_cmd events publish transcript.ready \
  --payload-json "{\"path\": \"$PROJECT_DIR/source/transcript.txt\", \"name\": \"transcript.txt\"}" \
  --source demo

section "Running the agent (live model calls; this takes a minute or two)"
echo "The agent stores the transcript as a content object, then derives a"
echo "summary and a published note through two recorded model transformations,"
echo "and selects the note as its final answer."
zeta_cmd run

section "What the trace store now recorded (zeta traces log)"
zeta_cmd traces log --session "$SESSION" --all --limit 40

section "Walking the provenance chain of the published note"
echo "Every id below comes from stored objects, queried with the zeta CLI."
echo

NOTE_ID="$(zeta_cmd traces tools --json --session "$SESSION" --limit 20 \
  | py 'import json,sys; rows=json.load(sys.stdin); print(next(r["input"]["object_id"] for r in rows if r.get("name")=="finish"))')"

NOTE_JSON="$(zeta_cmd traces show "$NOTE_ID" --json --session "$SESSION")"
NOTE_TEXT="$(echo "$NOTE_JSON" | py 'import json,sys; print(json.load(sys.stdin)["object"]["data"]["content"])')"
SUMMARY_ID="$(echo "$NOTE_JSON" | py 'import json,sys; print(json.load(sys.stdin)["object"]["links"][0])')"
RESPONSE2_ID="$(echo "$NOTE_JSON" | py 'import json,sys; print(json.load(sys.stdin)["object"]["links"][1])')"

SUMMARY_JSON="$(zeta_cmd traces show "$SUMMARY_ID" --json --session "$SESSION")"
SUMMARY_TEXT="$(echo "$SUMMARY_JSON" | py 'import json,sys; print(json.load(sys.stdin)["object"]["data"]["content"])')"
TRANSCRIPT_ID="$(echo "$SUMMARY_JSON" | py 'import json,sys; print(json.load(sys.stdin)["object"]["links"][0])')"
RESPONSE1_ID="$(echo "$SUMMARY_JSON" | py 'import json,sys; print(json.load(sys.stdin)["object"]["links"][1])')"

PROMPT2_ID="$(zeta_cmd traces show "$RESPONSE2_ID" --json --session "$SESSION" \
  | py 'import json,sys; print(json.load(sys.stdin)["object"]["links"][0])')"
PROMPT1_ID="$(zeta_cmd traces show "$RESPONSE1_ID" --json --session "$SESSION" \
  | py 'import json,sys; print(json.load(sys.stdin)["object"]["links"][0])')"

cat <<EOF
published note   $NOTE_ID
  "$NOTE_TEXT"
  stored inputs:  summary object + model response below

model call #2    prompt   $PROMPT2_ID
                 response $RESPONSE2_ID

summary          $SUMMARY_ID
  "$SUMMARY_TEXT"
  stored inputs:  transcript object + model response below

model call #1    prompt   $PROMPT1_ID
                 response $RESPONSE1_ID

transcript       $TRANSCRIPT_ID
  stored verbatim from source/transcript.txt (kind=document)
EOF

section "The exact model call that produced the summary (zeta traces show)"
echo "This prompt object is what the model actually received - note the"
echo "content_transform_input component carrying the transcript object:"
echo
zeta_cmd traces show "$PROMPT1_ID" --session "$SESSION"

section "The derivation tree around the note's transform call (zeta traces tree)"
NOTE_CALL_ID="$(zeta_cmd traces grep '"key":"note"' --kind tool_call --session "$SESSION" --limit 1 | awk '{print $1}')"
zeta_cmd traces tree "$NOTE_CALL_ID" --session "$SESSION" --depth 4

section "Every object reachable from the note (zeta traces closure)"
echo "The complete, self-contained audit set behind the published sentence:"
echo
zeta_cmd traces closure "$NOTE_ID" --session "$SESSION" \
  | py 'import json,sys,collections
objs=json.load(sys.stdin)["objects"]
kinds=collections.Counter(o["kind"] for o in objs)
for kind,count in sorted(kinds.items()): print(f"  {count:2d}  {kind}")
print(f"  total: {len(objs)} content-addressed objects")'

section "What just happened"
cat <<'EOF'
Provenance chain, as stored (not reconstructed):

  transcript.txt (seed file)
    -> content_node key=transcript   stored verbatim
    -> prompt                        the exact model request; the transcript
                                     object is one of its components
    -> assistant_message             the raw model response
    -> content_node key=summary      links to transcript + response
    -> prompt -> assistant_message   the second model call
    -> content_node key=note         the published sentence

Every id above is a b3: BLAKE3 content address. The note links to the
exact summary object and the exact model response that produced it; the
response links to the exact prompt; the prompt's components include the
content-addressed input the model saw. Change one byte anywhere and every
downstream address changes: unlike log lines, the chain cannot be edited
after the fact without breaking it. "Which model call produced this
sentence, from what input?" is answered by reading stored objects.
EOF

section "Inspect it yourself (run inside demo/content-provenance/work/pipeline)"
cat <<EOF
  uv run --project $REPO_ROOT zeta traces show $NOTE_ID --json --session $SESSION
  uv run --project $REPO_ROOT zeta traces show $PROMPT1_ID --session $SESSION
  uv run --project $REPO_ROOT zeta traces closure $NOTE_ID --session $SESSION
  uv run --project $REPO_ROOT zeta traces replay $PROMPT1_ID --session $SESSION --model codex --diff
EOF
