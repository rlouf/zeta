#!/usr/bin/env bash
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"
PROJECT="$DEMO_DIR/work/project"

ZETA() { uv run --project "$REPO_ROOT" zeta "$@"; }
PYRUN() { uv run --project "$REPO_ROOT" python -c "$@"; }
say() { printf '\n==> %s\n\n' "$1"; }

CASE_TEXT='I forgot my password. Please send me a password reset link for my own account.'

# The produced case.triaged event for one trigger: "id category reason..."
triage_result() {
  ZETA events list --type-prefix case.triaged --json | PYRUN '
import json, sys
for e in json.load(sys.stdin):
    if sys.argv[1] in (e.get("run_id") or ""):
        print(e["id"], e["payload"]["category"], e["payload"]["reason"])
' "$1"
}

# The model call that published the decision: "run_id prompt_object_id"
decision_prompt() {
  ZETA events list --type-prefix zeta.model_call --json | PYRUN '
import json, sys
for e in json.load(sys.stdin):
    p = e["payload"]
    names = [c["function"]["name"] for c in p.get("tool_calls") or []]
    if "publish_event" in names and sys.argv[1] in (e.get("run_id") or ""):
        print(e["run_id"], p["prompt_object_id"])
' "$1"
}

# The skill revision the run was pinned to, from its execution manifest.
skill_pin() {
  ZETA attempts list --json | PYRUN '
import json, sys
for a in json.load(sys.stdin):
    if a.get("run_id") == sys.argv[1]:
        skill = ((a.get("execution_manifest") or {}).get("skills") or {}).get("triage-policy") or {}
        if skill.get("object_id"):
            print(skill["object_id"])
            break
' "$1"
}

# Recover the exact policy bytes from a stored read tool_result component:
# strip the [path#tag] snapshot header and the line-number prefixes.
policy_from_prompt() {
  ZETA traces show "$1" --json | PYRUN '
import json, re, sys
data = json.load(sys.stdin)["object"]["data"]
inner = json.loads(data["message"]["content"])
text = inner["content"][0]["text"]
lines = text.splitlines(keepends=True)
sys.stdout.write("".join(re.sub(r"^\d+:", "", ln, count=1) for ln in lines[1:]))
'
}

# --- Scaffold a fresh project under work/ ---------------------------------
rm -rf "$DEMO_DIR/work"
mkdir -p "$PROJECT/agents/skills" "$PROJECT/agents/events" "$DEMO_DIR/work/tmp"
sed "s|__BASE_DIR__|$PROJECT|" "$DEMO_DIR/templates/case-triage.md.tmpl" \
  > "$PROJECT/agents/case-triage.md"
sed "s|__BASE_DIR__|$PROJECT|" "$DEMO_DIR/templates/policy-editor.md.tmpl" \
  > "$PROJECT/agents/policy-editor.md"
cp "$DEMO_DIR/templates/events/"*.json "$PROJECT/agents/events/"
cp "$DEMO_DIR/templates/triage-policy.md" "$PROJECT/agents/skills/triage-policy.md"

# All zeta state lives inside the work project, never in the repo root.
export ZETA_STATE_DIR="$PROJECT/.zeta"
cd "$PROJECT"

say "The setup"
echo "A case-triage agent classifies support cases by reading a policy skill"
echo "(agents/skills/triage-policy.md) and applying its rules exactly. A second"
echo "agent, policy-editor, amends that skill when a triage.correction event"
echo "confirms a mistake. The policy ships with an overbroad rule 1:"
echo
sed -n '6,8p' "$PROJECT/agents/skills/triage-policy.md"
echo
echo "A routine password-reset request will match it and be escalated as a"
echo "security incident."

# --- Run 1: the mistake ----------------------------------------------------
say "Run 1: publishing a routine password-reset case (live model call)"
echo "text: \"$CASE_TEXT\""
PUBLISH1=$(ZETA events publish case.opened \
  --source demo --idempotency-key case-001 \
  --payload-json "{\"case_id\": \"case-001\", \"text\": \"$CASE_TEXT\"}")
echo "$PUBLISH1"
EVT1=$(echo "$PUBLISH1" | awk '{print $NF}')
ZETA run

TRIAGE1=$(triage_result "$EVT1")
CAT1=$(echo "$TRIAGE1" | awk '{print $2}')
say "Run 1 result: the mistake"
echo "produced event: $(echo "$TRIAGE1" | awk '{print $1}')"
echo "category:       $CAT1"
echo "reason:         $(echo "$TRIAGE1" | cut -d' ' -f3-)"
if [ "$CAT1" != "security_incident" ]; then
  echo "unexpected: run 1 should have mislabeled the case as security_incident" >&2
  exit 1
fi
echo
echo "A password-reset request was escalated as a security incident. Wrong."

# --- Revision identity for run 1 -------------------------------------------
read -r RUN1 PROMPT1 <<< "$(decision_prompt "$EVT1")"
SKILL_OID1=$(skill_pin "$RUN1" | head -1)

say "Which policy revision governed run 1? Provable from durable state"
echo "The run's execution manifest (recorded on the attempt) pins the skill to"
echo "a content-addressed object:"
echo
echo "  run:                 $RUN1"
echo "  skills.triage-policy: $SKILL_OID1"
echo
echo "And the exact policy text the model saw sits inside the stored decision"
echo "prompt as a content-addressed tool_result component:"
echo
echo "\$ zeta traces show ${PROMPT1:0:11}"
ZETA traces show "$PROMPT1"
TR1=$(ZETA traces show "$PROMPT1" | awk '$2 == "tool_result" {print $1; exit}')
echo
echo "Policy revision A, read back from the prompt store (component $TR1):"
echo
policy_from_prompt "$TR1" | tee "$DEMO_DIR/work/tmp/policy-run1.md" | sed -n '6,8p'

# --- The correction: an agent amends its own policy ------------------------
say "The correction arrives; the policy-editor agent amends the skill (live model call)"
CORRECTION_PAYLOAD=$(cat <<'EOF'
{"case_id": "case-001", "wrong_category": "security_incident", "correct_category": "account_request", "new_rule": "1. Cases that report unauthorized access, stolen or leaked credentials, or a\n   suspected breach are classified as `security_incident`. A routine password\n   reset or account unlock request for the requester's own account is\n   `account_request`, never `security_incident`."}
EOF
)
PUBLISHC=$(ZETA events publish triage.correction \
  --source review-desk --idempotency-key correction-001 \
  --payload-json "$CORRECTION_PAYLOAD")
echo "$PUBLISHC"
EVTC=$(echo "$PUBLISHC" | awk '{print $NF}')
ZETA run

if ! grep -q "unauthorized access" "$PROJECT/agents/skills/triage-policy.md" \
   || grep -q "Any case that mentions a password" "$PROJECT/agents/skills/triage-policy.md"; then
  echo "unexpected: the policy-editor did not amend rule 1 as instructed" >&2
  exit 1
fi

AMEND=$(ZETA events list --type-prefix triage.policy.amended --json | PYRUN '
import json, sys
for e in json.load(sys.stdin):
    if sys.argv[1] in (e.get("run_id") or ""):
        print(e["id"], e["run_id"], e["payload"]["summary"])
' "$EVTC")
AMEND_ID=$(echo "$AMEND" | awk '{print $1}')
RUN_EDIT=$(echo "$AMEND" | awk '{print $2}')

say "The amendment is an agent run on the record, not a silent file mutation"
echo "summary: $(echo "$AMEND" | cut -d' ' -f3-)"
echo
echo "\$ zeta ps $RUN_EDIT"
ZETA ps "$RUN_EDIT"
echo
echo "\$ zeta events chain $AMEND_ID"
ZETA events chain "$AMEND_ID"
echo
echo "The causal chain runs from the human correction event through the editor"
echo "run to the amendment notice. Who changed the policy, why, and when is a"
echo "journal query."

# --- Run 2: the fix, with new revision identity ----------------------------
say "Run 2: a fresh equivalent case against the amended policy (live model call)"
PUBLISH2=$(ZETA events publish case.opened \
  --source demo --idempotency-key case-002 \
  --payload-json "{\"case_id\": \"case-002\", \"text\": \"$CASE_TEXT\"}")
echo "$PUBLISH2"
EVT2=$(echo "$PUBLISH2" | awk '{print $NF}')
ZETA run

TRIAGE2=$(triage_result "$EVT2")
CAT2=$(echo "$TRIAGE2" | awk '{print $2}')
say "Run 2 result: corrected behavior"
echo "produced event: $(echo "$TRIAGE2" | awk '{print $1}')"
echo "category:       $CAT2"
echo "reason:         $(echo "$TRIAGE2" | cut -d' ' -f3-)"
if [ "$CAT2" != "account_request" ]; then
  echo "unexpected: run 2 should have classified the case as account_request" >&2
  exit 1
fi

read -r RUN2 PROMPT2 <<< "$(decision_prompt "$EVT2")"
SKILL_OID2=$(skill_pin "$RUN2" | head -1)
TR2=$(ZETA traces show "$PROMPT2" | awk '$2 == "tool_result" {print $1; exit}')
policy_from_prompt "$TR2" > "$DEMO_DIR/work/tmp/policy-run2.md"

say "Revision identity: run 2 was governed by a different durable revision"
echo "  run 1 skill object:  $SKILL_OID1"
echo "  run 2 skill object:  $SKILL_OID2"
echo "  run 1 prompt component: $TR1"
echo "  run 2 prompt component: $TR2"
if [ "$SKILL_OID1" = "$SKILL_OID2" ] || [ "$TR1" = "$TR2" ]; then
  echo "unexpected: the two runs should pin different skill revisions" >&2
  exit 1
fi
echo
echo "Every project revision that ever ran is also journaled with the skill"
echo "revision it pinned (runtime.project_revision.recorded):"
echo
ZETA events list --type-prefix runtime.project_revision --json | PYRUN '
import json, sys
for e in json.load(sys.stdin):
    p = e["payload"]
    skill = (p["manifest"].get("skills") or {}).get("triage-policy") or {}
    gen = p["revision_id"][:28]
    oid = skill.get("object_id", "?")
    print("  generation " + gen + "...  triage-policy " + oid)
'

# --- Operator loop A: diff the revisions -----------------------------------
say "Operator loop, part 1: diff the two stored decision prompts"
echo "\$ zeta traces diff ${PROMPT1:0:11} ${PROMPT2:0:11}"
ZETA traces diff "$PROMPT1" "$PROMPT2"
echo
echo "(Per-event sessions keep this component pairing correct; in a shared"
echo "session the pairing is wrong today — zeta issue #32.)"
echo
echo "The same delta as a plain unified diff of the two stored revisions:"
echo
diff -u "$DEMO_DIR/work/tmp/policy-run1.md" "$DEMO_DIR/work/tmp/policy-run2.md" \
  | sed -e "s|$DEMO_DIR/work/tmp/policy-run1.md|policy@run1 (from prompt store)|" \
        -e "s|$DEMO_DIR/work/tmp/policy-run2.md|policy@run2 (from prompt store)|" || true

# --- Operator loop B: restore revision A from the prompt store -------------
say "Operator loop, part 2: restore revision A from the prompt store alone"
echo "No filesystem backup, no git. The bytes come out of run 1's stored"
echo "tool_result, and the snapshot tag recorded at read time verifies them:"
echo
ZETA traces show "$TR1" --json | PYRUN '
import json, re, sys
from zeta.addresses import content_address
data = json.load(sys.stdin)["object"]["data"]
inner = json.loads(data["message"]["content"])
text = inner["content"][0]["text"]
lines = text.splitlines(keepends=True)
recorded_tag = lines[0].strip()[:-1].rsplit("#", 1)[1]
body = "".join(re.sub(r"^\d+:", "", ln, count=1) for ln in lines[1:])
restored_tag = content_address(body.encode()).split(":", 1)[1][:8]
if restored_tag != recorded_tag:
    sys.exit(f"restored bytes hash {restored_tag} != recorded snapshot tag {recorded_tag}")
with open(sys.argv[1], "w", encoding="utf-8", newline="") as fh:
    fh.write(body)
print(f"restored {len(body)} bytes to {sys.argv[1]}")
print(f"blake3 tag of restored bytes: {restored_tag} == tag recorded at read time: {recorded_tag}")
' "$PROJECT/agents/skills/triage-policy.md"

say "Run 3: rerun the same case under the restored revision (live model call)"
PUBLISH3=$(ZETA events publish case.opened \
  --source demo --idempotency-key case-003 \
  --payload-json "{\"case_id\": \"case-003\", \"text\": \"$CASE_TEXT\"}")
echo "$PUBLISH3"
EVT3=$(echo "$PUBLISH3" | awk '{print $NF}')
ZETA run

TRIAGE3=$(triage_result "$EVT3")
CAT3=$(echo "$TRIAGE3" | awk '{print $2}')
read -r RUN3 PROMPT3 <<< "$(decision_prompt "$EVT3")"
SKILL_OID3=$(skill_pin "$RUN3" | head -1)

say "Run 3 result: behavior reverted, and identity proves the restore is exact"
echo "category:            $CAT3"
echo "reason:              $(echo "$TRIAGE3" | cut -d' ' -f3-)"
echo "run 3 skill object:  $SKILL_OID3"
echo "run 1 skill object:  $SKILL_OID1"
if [ "$CAT3" != "security_incident" ]; then
  echo "unexpected: run 3 should have reverted to the security_incident label" >&2
  exit 1
fi
if [ "$SKILL_OID3" != "$SKILL_OID1" ]; then
  echo "unexpected: the restored revision should have run 1's content address" >&2
  exit 1
fi
echo
echo "Run 3 pins the SAME content address as run 1 — the restore recovered the"
echo "revision itself, not a lookalike. Content addressing makes byte-identity"
echo "checkable instead of assumed."

say "What just happened"
echo "The agent amended its own policy skill after a confirmed mistake, and"
echo "nothing about that was ephemeral:"
echo "  explicit   - the correction is an event; the edit is an agent run"
echo "  immutable  - each revision is a content-addressed object; runs pin it"
echo "               in their execution manifest and carry it in stored prompts"
echo "  reversible - any historical revision is recoverable from the prompt"
echo "               store alone, hash-verified"
echo "  visible    - which revision governed which run is a query, not a guess"

say "Inspect it yourself (from $PROJECT)"
echo "  export ZETA_STATE_DIR=$PROJECT/.zeta"
echo "  uv run --project $REPO_ROOT zeta events chain $AMEND_ID"
echo "  uv run --project $REPO_ROOT zeta traces show $TR1"
echo "  uv run --project $REPO_ROOT zeta traces diff $PROMPT1 $PROMPT2"
echo "  uv run --project $REPO_ROOT zeta attempts list --json | python3 -m json.tool | less"
