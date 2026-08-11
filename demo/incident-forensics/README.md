# Incident forensics: root-cause a wrong agent output from durable state

An agent classifies a customer's refund request as spam. Instead of grepping
logs and guessing, this demo walks Zeta's durable state from the bad output
back to its cause: the causal event chain leads to the run, the run leads to
the exact stored prompt the model saw, and inside that prompt sits the bad
rule someone shipped in a shared skill file. Fix the skill, rerun on an
identical trigger, and `zeta traces diff` proves component-by-component that
the only input that changed between the wrong run and the right run is the
skill content.

## Why other frameworks can't do this

Every prompt Zeta sends is stored as a content-addressed object graph: system
prompt, user objective, tool results, and tool descriptors are separate linked
components, and the prompt object records the hash of the exact final payload.
That is what makes `zeta traces diff` a component-level diff between two runs
rather than a text diff of log lines.

- LangGraph and CrewAI can log prompts if you wire up a tracing callback
  (LangSmith and similar), but the trace is telemetry bolted onto the run, not
  the runtime's own durable state. You get flat request dumps, not a component
  graph you can diff, and the causal link from a produced event back through
  the attempt to the trigger is not a first-class query.
- Temporal gives you the durable event-history half: it can replay a workflow
  and show you the activity inputs. But prompt internals are opaque activity
  arguments; there is no notion of prompt components, so "diff the two prompts
  and show me which component changed" is not an operation it has.
- n8n keeps execution logs per node run, good for seeing payloads, but the
  same limits apply: no causal chain across executions and no structured
  prompt store.

## How to run

Prerequisites:

- Python 3.11+ and `uv`
- Codex CLI logged in (`codex login`) — the built-in `codex` model profile is
  used for both agent runs

```sh
./run.sh
```

The script makes live model calls (two agent runs, three model turns each);
a full run takes roughly 20-40 seconds. All state is created under `work/`,
which is recreated on every run and git-ignored.

## What you'll see and how to interpret it

- `==> The setup` — the scaffolded project: a support-triage agent whose
  prompt tells it to read `agents/skills/refund-policy.md` and apply its rules
  exactly. The skill ships with a deliberately bad rule: refund requests are
  classified as `spam`.
- `==> Publishing the trigger event` / `==> Running the agent` — a clear
  refund request is published as a durable `support.message.received` event
  and `zeta run` drains the queue.
- `==> The incident` — the agent published `support.triage.completed` with
  `category: spam`. This is the wrong output an on-call engineer would start
  from.
- `==> Forensics 1/4` — `zeta events chain` on the bad event prints the causal
  chain: trigger event, attempt, produced event. `zeta events root` collapses
  it to the root cause event. No log grepping; causality is a query over the
  journal.
- `==> Forensics 2/4` — `zeta ps <run-id>` shows the run that produced the
  event: agent, trigger, session, timing.
- `==> Forensics 3/4` — `zeta traces log` lists every prompt and assistant
  message the run recorded; the decision prompt is identified from the journal
  (each model-call event records the id of the exact stored prompt object).
- `==> Forensics 4/4` — `zeta traces show` prints the decision prompt's
  components, then the `tool_result` component: the skill file content,
  byte-exact and content-hashed, sitting inside the stored prompt. This is
  the root cause, read from state rather than reconstructed from logs.
- `==> Fixing the skill file` / second run — the rule is corrected, a second
  trigger with identical text (new event id, new idempotency key) is
  published, and the agent now returns `category: refund_request`.
- `==> Proof` — `zeta traces diff` compares the two decision prompts.
  Unchanged: system prompt, tool descriptors, content manifest, user message.
  Changed: the assistant read call (provider tool-call id only) and the
  `tool_result` holding the skill text, where the unified diff shows exactly
  the rule flip. Same trigger text, same agent, same model; the store proves
  the skill was the only substantive input change.

The script asserts both classifications (`spam` then `refund_request`) and
exits non-zero if a model run deviates; the task is designed so the skill rule
dominates the decision, and both outcomes were stable across verification
runs.

## Where else this principle applies

- Prompt-injection incident response: when an agent misbehaves after ingesting
  hostile content, the stored prompt graph shows exactly which component
  carried the injected text and which runs consumed it.
- Regression bisection for prompt changes: diff the decision prompts of a run
  before and after a system-prompt or template edit to see precisely which
  component changed, instead of eyeballing two multi-kilobyte request dumps.
- Compliance and audit: for a regulated decision (credit, moderation, triage),
  the content-addressed prompt is durable evidence of what the model was shown
  when it decided.
- Shared-skill governance: skills are files many agents opt into; when one is
  edited badly, the event chain plus prompt store identifies every run that
  decided under the bad version.
- Model migration validation: replay a stored prompt against a new model
  profile (`zeta traces replay --diff`) and compare answers with the inputs
  pinned, so behavior changes are attributable to the model rather than
  drifting context.
