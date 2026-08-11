# Skill evolution: a self-tuning agent with durable revision history

A triage agent applies a policy skill and mislabels a routine password-reset
request as a security incident. A human files a `triage.correction` event; a
policy-editor agent amends the skill file — the agent system tunes its own
policy. This demo shows that the self-modification comes with history instead
of handcuffs: every policy revision is a content-addressed object, every run's
execution manifest pins the revision that governed it, the exact policy text
the model saw sits inside the stored prompt, the two revisions can be diffed
from durable state, and any historical revision can be restored byte-exactly
from the prompt store alone — no filesystem backup, no git.

## Why other frameworks can't do this

The claim is not "agents can edit their own prompts" — anything can write a
file. The claim is that in Zeta the revision that governed each run is part of
the runtime's own durable state, in three independent places:

- each run's execution manifest (recorded on the attempt) pins
  `skills.triage-policy` to a content address;
- each project generation is journaled (`runtime.project_snapshot.recorded`)
  with the skill body and its content address, verified on reload;
- the policy text the model actually read is stored as a content-addressed
  `tool_result` component inside the decision prompt.

So "which policy version produced this decision" is a query, a diff is an
operation on stored objects, and a restore can be hash-verified against what
was recorded at read time.

- LangGraph and CrewAI let an agent rewrite its own prompt or a RAG'd policy
  file, but the framework does not record which version any given run saw.
  With LangSmith you get request dumps as telemetry; version identity of a
  shared policy file across runs is something you build yourself.
- Temporal durably records activity inputs and can replay them, so it covers
  the "what arguments did this activity get" half. But a policy file read
  inside an activity is opaque bytes in application land: no content-addressed
  identity, no cross-run pinning, no component diff, and restoring an old
  revision from workflow history is manual archaeology.
- n8n keeps per-node execution logs, but a file both read and written by
  different workflows has no version identity connecting the logs.

## How to run

Prerequisites:

- Python 3.11+ and `uv`
- Codex CLI logged in (`codex login`) — the built-in `codex` model profile is
  used for all runs

```sh
./run.sh
```

The script makes live model calls (four agent runs: three triage, one edit);
a full run takes roughly 1-3 minutes. All state is created under `work/`,
which is recreated on every run and git-ignored.

## What you'll see and how to interpret it

- `==> The setup` — a case-triage agent reads
  `agents/skills/triage-policy.md` and applies it; a policy-editor agent
  amends that file on `triage.correction` events. Rule 1 is deliberately
  overbroad: anything mentioning a password is a `security_incident`.
- `==> Run 1` — a routine password-reset case is published and mislabeled
  `security_incident` (asserted).
- `==> Which policy revision governed run 1?` — the run's execution manifest
  pins `skills.triage-policy` to a content address, and `zeta traces show` on
  the decision prompt reveals the policy text itself as a content-addressed
  `tool_result` component. That pair is revision identity for run 1.
- `==> The correction arrives` — a `triage.correction` event carries the exact
  replacement rule; the policy-editor agent rewrites the skill file (the
  script asserts the edit landed). `zeta ps` shows the editor's run and
  `zeta events chain` walks from the amendment notice back through the editor
  run to the human correction event: the self-edit is on the causal record.
- `==> Run 2` — a fresh equivalent case is now labeled `account_request`
  (asserted). Its manifest pins a different skill content address, its prompt
  carries a different `tool_result` component, and the journal now holds two
  project generations, each pinning its own revision.
- `==> Operator loop, part 1` — `zeta traces diff` compares the two decision
  prompts component by component; the only substantive change is the policy
  `tool_result`, shown as a unified diff. A plain `diff -u` of the two
  revisions extracted from the prompt store shows the same delta.
- `==> Operator loop, part 2` — the restore. Run 1's exact policy bytes are
  reconstructed from its stored `tool_result` (the read tool records a
  `[path#tag]` snapshot header whose tag is the blake3 short hash of the file
  bytes; the script recomputes the hash of the reconstructed bytes and
  requires a match) and written back over the skill file.
- `==> Run 3` — the same case now mislabels again (`security_incident`,
  asserted): behavior reverted. Stronger: run 3's execution manifest pins the
  same content address as run 1's, proving the restored file is the original
  revision itself, not an approximation.

Honesty notes:

- Skill bodies are never injected into prompts by the `skills:` frontmatter
  (zeta issue #33); the agent is instructed to `read` the file, which is what
  places the policy text in the stored prompt. The `skills:` declaration still
  pins revision identity in the manifest either way.
- The two decision prompts pair correctly in `zeta traces diff` because the
  agents use per-event sessions (the default). In a shared session the
  component pairing is currently wrong (zeta issue #32).
- The policy-editor is a live model run given the exact replacement text, so
  the amended file could in principle deviate; the script asserts the rule
  flip and both classifications, and exits non-zero on any deviation.

## Where else this principle applies

- Self-improving prompt/policy loops: any system that lets agents tune their
  own instructions needs exactly this — an audit trail of who changed what,
  which runs each version governed, and a way back.
- Compliance for adaptive automation: when a moderation or credit policy is
  machine-amended, regulators ask "what policy was in force for this
  decision" — here that is a stored, hash-verified object, not a changelog
  entry.
- Fleet-wide skill governance: many agents opting into one shared skill; the
  manifest pins tell you exactly which runs decided under which revision when
  a bad edit ships.
- Canarying policy changes: pin one agent to the new revision, compare
  decisions across the two content addresses on live traffic.
- Disaster recovery for config: the prompt store doubles as a provable backup
  of every config file an agent ever read, recoverable byte-exactly with the
  hash to prove it.
