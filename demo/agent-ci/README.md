# CI for agent definitions

An edit to an agent's prompt is a code change with behavioral consequences,
but most teams ship it on vibes. This demo reviews an agent edit the way CI
reviews code: it replays a frozen corpus of the agent's real past trigger
events through the candidate definition in a scratch environment, and diffs
the outcomes against the recorded production baseline. A cosmetic rewording
comes back GREEN; a real rules change comes back RED with a per-ticket
old -> new diff. The diff — not a score, not a log excerpt — is the review
artifact. The corpus is yesterday's actual traffic, so these are golden
regression tests from production history, for free.

The mechanism this depends on: Zeta's journal stores every trigger event as a
durable, typed record (type, source, payload, idempotency key), and every
agent outcome as a schema-validated published event. `zeta events list --json`
reconstructs the exact production inputs; republishing them into a scratch
project with its own `ZETA_STATE_DIR` re-runs history against the candidate
without touching production state.

## Why other frameworks can't do this

- **LangGraph / CrewAI**: inputs to a run live in whatever logging you bolted
  on (LangSmith traces, callbacks). Traces are observability artifacts —
  lossy, unvalidated, and not designed to be re-ingested as triggers. To
  replay yesterday's traffic you would have to reconstruct the trigger
  payloads from log lines and hand-write the harness that feeds them back in.
  Here the journal *is* the corpus, byte-exact, and `events publish` is the
  replay mechanism.
- **Temporal**: comes closest — it stores full workflow histories and has
  replay testing. But Temporal replay checks that *deterministic workflow
  code* still matches recorded histories; it raises nondeterminism errors on
  divergence rather than producing outcomes to diff, and it never re-executes
  activities (the model calls are exactly what you want to re-execute against
  the edited agent). You could build this demo's loop on Temporal, but you
  would be writing the corpus extraction, scratch isolation, and outcome
  diffing yourself.
- **n8n**: keeps execution logs and can re-run a single past execution, but
  re-running a day of executions against an *edited* workflow and diffing
  structured outcomes is not a supported artifact — and outputs are untyped
  JSON, so "the outcome changed" has no schema to anchor it.

What Zeta contributes is that both sides of the diff are first-class durable
records: triggers are replayable events, outcomes are schema-validated events
(`agents/events/ticket.triaged.json`), and isolation is one environment
variable.

## How to run

Prerequisites:

- Python 3.11+ and `uv`
- The Codex CLI, logged in once with `codex login`

```sh
cd demo/agent-ci
./run.sh
```

The script makes live model calls: the 4-ticket corpus runs three times
(production baseline, candidate A, candidate B) — 12 small calls, a few
minutes total. All state lives under `work/`, which is recreated each run.

## What you'll see and how to interpret it

1. **Production**: four `ticket.received` events are published and a triage
   agent with four crisp keyword rules processes them, publishing one typed
   `ticket.triaged` event each. Because `zeta run` currently exits 0 even
   when an attempt fails (issue #30), the script explicitly checks
   `zeta attempts list` after every run.
2. **Freezing the corpus**: the trigger events are read back from the
   journal with `zeta events list --type-prefix ticket.received --json` and
   frozen into `work/corpus.json` — payloads, sources, and idempotency keys
   exactly as production received them.
3. **Recording the baseline**: the agent's published `ticket.triaged` events
   are reduced to a ticket -> priority map and asserted
   (t-101 critical, t-102 high, t-103 medium, t-104 low).
4. **CI run A (cosmetic edit)**: the agent prompt is reworded without
   changing the rules. A fresh scratch project (`work/ci-a`, own
   `ZETA_STATE_DIR`) gets the edited definition, the corpus is republished
   verbatim, and the outcome diff comes back **GREEN**: every ticket got the
   same priority. Evidence the edit is safe to ship.
5. **CI run B (rules change)**: rule 2 is changed so security tickets
   escalate to critical. Same replay, and the diff comes back **RED**:
   exactly t-102 changed, `high -> critical`, everything else unchanged.
6. **The review artifact**: a table of corpus event -> baseline outcome ->
   candidate outcome -> changed?. That table is what a reviewer would look
   at on the PR: run B is not a "test failure", it is the precise behavioral
   consequence of the edit on real traffic, to be approved or rejected as a
   decision.

The script asserts all three outcome sets, so it exits non-zero if any run
misbehaves.

### Honest limits

- The model is still a sampler. The demo's rules are deliberately crisp so
  outcomes are deterministic in practice; on fuzzier agents you would expect
  occasional benign divergence and treat the diff as review input, possibly
  replaying N times or diffing only schema-constrained fields (as here: the
  priority is an enum, so the diffable surface is exactly the decision).
- Both baseline and candidates run against the same built-in `codex` model
  profile — the variable under test is the agent definition, not the model.
- The CLI does not validate manually published payloads against the event
  schema (issue #24); the corpus here conforms by construction, and the
  agent-published outcomes *are* validated by the runtime.
- The CI pipeline integration (freezing nightly, running on PRs, posting the
  table as a comment) is described in the script's closing section, not
  built.

## Where else this principle applies

- **Prompt/skill changes on support and ops agents**: any edit to a triage,
  routing, or escalation agent gets reviewed against last week's real
  tickets before it ships, with the outcome table attached to the PR.
- **Model migrations**: freeze the corpus once, replay against a candidate
  model profile instead of a candidate definition — same diff, different
  variable (see the `shadow-eval` and `model-replay-diff` demos for the
  prompt-level version).
- **Extraction and classification pipelines**: schema-validated outputs
  (enums, typed fields) make the diff exact — a changed extraction prompt
  shows precisely which documents now parse differently.
- **Compliance and audit**: "what would the new policy have decided on last
  quarter's cases" is the same replay with a bigger corpus, and the journal
  proves the inputs were the real cases.
- **Incident postmortems**: after fixing an agent (see `incident-forensics`),
  replay the incident-day corpus through the fix to prove the bad outcome is
  gone and nothing else moved.
