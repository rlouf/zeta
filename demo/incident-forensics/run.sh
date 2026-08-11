#!/usr/bin/env bash
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"
PROJECT="$DEMO_DIR/work/project"

ZETA() { uv run --project "$REPO_ROOT" zeta "$@"; }
PYRUN() { uv run --project "$REPO_ROOT" python -c "$@"; }
say() { printf '\n==> %s\n\n' "$1"; }

MESSAGE='Hi, I bought the Pro plan last week but it is not what I need. Please refund my payment. Order #4821.'

# --- Scaffold a fresh project under work/ ---------------------------------
rm -rf "$DEMO_DIR/work"
mkdir -p "$PROJECT/agents/skills" "$PROJECT/agents/events"
sed "s|__BASE_DIR__|$PROJECT|" "$DEMO_DIR/templates/support-triage.md.tmpl" \
  > "$PROJECT/agents/support-triage.md"
cp "$DEMO_DIR/templates/events/"*.json "$PROJECT/agents/events/"
cp "$DEMO_DIR/templates/refund-policy.bad.md" \
  "$PROJECT/agents/skills/refund-policy.md"

# All zeta state lives inside the work project, never in the repo root.
export ZETA_STATE_DIR="$PROJECT/.zeta"
cd "$PROJECT"

say "The setup"
echo "A support-triage agent classifies inbound support messages. It is told to"
echo "read a shared skill file (agents/skills/refund-policy.md) and apply its"
echo "rules exactly. Someone shipped a bad rule into that skill:"
echo
sed -n '6,8p' "$PROJECT/agents/skills/refund-policy.md"

# --- Run 1: the incident ---------------------------------------------------
say "Publishing the trigger event (a clear refund request)"
echo "text: \"$MESSAGE\""
PUBLISH1=$(ZETA events publish support.message.received \
  --source demo --idempotency-key msg-001 \
  --payload-json "{\"text\": \"$MESSAGE\", \"customer\": \"dana@example.com\"}")
echo "$PUBLISH1"
EVT1=$(echo "$PUBLISH1" | awk '{print $NF}')

say "Running the agent (live model call)"
ZETA run

TRIAGE1=$(ZETA events list --type-prefix support.triage --json | PYRUN '
import json, sys
for e in json.load(sys.stdin):
    if sys.argv[1] in (e.get("run_id") or ""):
        print(e["id"], e["payload"]["category"], e["payload"]["reason"])
' "$EVT1")
TRIAGE1_ID=$(echo "$TRIAGE1" | awk '{print $1}')
CAT1=$(echo "$TRIAGE1" | awk '{print $2}')

say "The incident: the agent produced a wrong output"
echo "produced event: $TRIAGE1_ID"
echo "category:       $CAT1"
echo "reason:         $(echo "$TRIAGE1" | cut -d' ' -f3-)"
if [ "$CAT1" != "spam" ]; then
  echo "unexpected: run 1 should have classified the refund request as spam" >&2
  exit 1
fi
echo
echo "A refund request was classified as spam. Time for forensics."

# --- Forensics: walk the durable state back to the cause -------------------
say "Forensics 1/4: causal chain from the bad event back to its root"
echo "\$ zeta events chain $TRIAGE1_ID"
ZETA events chain "$TRIAGE1_ID"
echo
echo "\$ zeta events root $TRIAGE1_ID"
ZETA events root "$TRIAGE1_ID"
echo
echo "The bad classification traces back, hop by hop, to the trigger event."

# The journal records every model call with the id of the exact stored prompt.
read -r RUN1 PROMPT1 <<< "$(ZETA events list --type-prefix zeta.model_call --json | PYRUN '
import json, sys
for e in json.load(sys.stdin):
    p = e["payload"]
    names = [c["function"]["name"] for c in p.get("tool_calls") or []]
    if "publish_event" in names and sys.argv[1] in (e.get("run_id") or ""):
        print(e["run_id"], p["prompt_object_id"])
' "$EVT1")"

say "Forensics 2/4: the run that produced it"
echo "\$ zeta ps $RUN1"
ZETA ps "$RUN1"

SES1="agent/support-triage/$EVT1"
say "Forensics 3/4: the run's prompt trace"
echo "\$ zeta traces log --session $SES1"
ZETA traces log --session "$SES1"
echo
echo "The decision prompt (the one whose answer published the event) is:"
echo "  $PROMPT1"

say "Forensics 4/4: the exact prompt the model saw"
echo "\$ zeta traces show ${PROMPT1:0:11}"
ZETA traces show "$PROMPT1"
TR1=$(ZETA traces show "$PROMPT1" | awk '$2 == "tool_result" {print $1; exit}')
echo
echo "The tool_result component ($TR1) is the skill file the agent read,"
echo "stored byte-exact inside the prompt:"
echo
echo "\$ zeta traces show $TR1"
ZETA traces show "$TR1" --json | PYRUN '
import json, sys
data = json.load(sys.stdin)["object"]["data"]
inner = json.loads(data["message"]["content"])
print(inner["content"][0]["text"])
'
echo
echo "Root cause found: the stored prompt contains the bad skill rule"
echo "\"Messages that ask for a refund ... are classified as spam\"."
echo "Not a guess from logs: this is the byte-exact prompt, content-addressed."

# --- The fix and the proof -------------------------------------------------
say "Fixing the skill file"
diff -u "$DEMO_DIR/templates/refund-policy.bad.md" \
        "$DEMO_DIR/templates/refund-policy.good.md" | sed -n '4,20p' || true
cp "$DEMO_DIR/templates/refund-policy.good.md" \
  "$PROJECT/agents/skills/refund-policy.md"

say "Publishing a second, identical refund request (new id, new idempotency key)"
PUBLISH2=$(ZETA events publish support.message.received \
  --source demo --idempotency-key msg-002 \
  --payload-json "{\"text\": \"$MESSAGE\", \"customer\": \"dana@example.com\"}")
echo "$PUBLISH2"
EVT2=$(echo "$PUBLISH2" | awk '{print $NF}')

say "Running the agent again (live model call)"
ZETA run

TRIAGE2=$(ZETA events list --type-prefix support.triage --json | PYRUN '
import json, sys
for e in json.load(sys.stdin):
    if sys.argv[1] in (e.get("run_id") or ""):
        print(e["id"], e["payload"]["category"], e["payload"]["reason"])
' "$EVT2")
CAT2=$(echo "$TRIAGE2" | awk '{print $2}')

say "The rerun: same input, fixed skill"
echo "produced event: $(echo "$TRIAGE2" | awk '{print $1}')"
echo "category:       $CAT2"
echo "reason:         $(echo "$TRIAGE2" | cut -d' ' -f3-)"
if [ "$CAT2" != "refund_request" ]; then
  echo "unexpected: run 2 should have classified the message as refund_request" >&2
  exit 1
fi

read -r RUN2 PROMPT2 <<< "$(ZETA events list --type-prefix zeta.model_call --json | PYRUN '
import json, sys
for e in json.load(sys.stdin):
    p = e["payload"]
    names = [c["function"]["name"] for c in p.get("tool_calls") or []]
    if "publish_event" in names and sys.argv[1] in (e.get("run_id") or ""):
        print(e["run_id"], p["prompt_object_id"])
' "$EVT2")"

say "Proof: diff the two decision prompts, component by component"
echo "\$ zeta traces diff ${PROMPT1:0:11} ${PROMPT2:0:11}"
ZETA traces diff "$PROMPT1" "$PROMPT2"

say "How to read that diff"
echo "Both prompts are stored object graphs, so the diff is component-level:"
echo "  = unchanged: system prompt, tool descriptors, content manifest, and the"
echo "    user message (the two trigger events carried identical text)."
echo "  ~ assistant_message: same read call, different provider tool-call id."
echo "  ~ tool_result: the skill file's content. The unified diff shows the one"
echo "    substantive change between the two runs: rule 1 flipped from"
echo "    'classified as \`spam\`' to 'classified as \`refund_request\`'."
echo
echo "Same trigger text, same agent, same model profile. The only input that"
echo "changed between the wrong run and the right run is the skill component,"
echo "and the prompt store proves it."

say "Inspect it yourself (from $PROJECT)"
echo "  export ZETA_STATE_DIR=$PROJECT/.zeta"
echo "  uv run --project $REPO_ROOT zeta events chain $TRIAGE1_ID"
echo "  uv run --project $REPO_ROOT zeta traces show $PROMPT1"
echo "  uv run --project $REPO_ROOT zeta traces diff $PROMPT1 $PROMPT2"
echo "  uv run --project $REPO_ROOT zeta traces grep 'classified as' --session $SES1"
