# Working memory with commit semantics

A long-horizon agent accumulates scratch work while it thinks, and only its
validated conclusion becomes durable memory. This demo shows Zeta's content
workspace doing exactly that: an agent stores raw measurements in **session**
scope and its conclusion in **agent** scope through the `transform_content`
tool. Nothing becomes durable until the run succeeds — the runtime applies the
promotions inside the attempt-completion transaction and journals each one as
a `runtime.content.promoted` event. Every scope's memory is a versioned head
(a ref moved by compare-and-swap over content-addressed revisions), so a stale
computation can never overwrite newer state. Memory here is a commit, not a
RAG blob that silently degrades.

## Why other frameworks can't do this

- **LangGraph** checkpoints graph state and offers a memory store, but writes
  to the store are plain puts: a slow, outdated branch can overwrite a newer
  value, and scratch state is not separated from durable state by a success
  boundary.
- **CrewAI** memory is embedding-backed recall. There is no revision head, no
  conditional write, and no transactional "only if the task succeeded" rule.
- **Temporal** gives you durable workflow state with strong versioning — that
  half it does well — but its state is per-workflow-execution. It has no
  agent-scoped memory shared across executions with optimistic concurrency,
  and no model-facing tool that stages writes for commit at success.
- **n8n** passes JSON between nodes; persistent memory is whatever database
  you bolt on, with whatever race conditions it has.

Zeta's combination — model-facing staging tool, scope isolation
(run/session/agent), promotion only inside the success transaction, and CAS on
every head move — is what none of them offer together.

## How to run

Prerequisites:

- Python 3.11+ and `uv`
- Codex CLI logged in (`codex login`) — the demo makes **live model calls**
  (two small agent runs, roughly 10–60 s each)

```sh
./run.sh
```

Everything runs inside `work/`, which is recreated on each run. The script
exports `ZETA_STATE_DIR` so the demo cannot touch any other Zeta state.

## What you'll see and how to interpret it

1. **Scaffolding** — `zeta new` creates a project; the demo replaces the
   scaffolded agent with `researcher`, which accepts a custom `research.task`
   event and uses `session: per-event`, so every event starts a fresh session.
2. **Run 1 (session A)** — the agent receives raw latency measurements. It
   calls `transform_content` twice: measurements go to key
   `scratch/measurements` in **session** scope, its one-line conclusion to
   `conclusions/latency` in **agent** scope. Both are staged during the run
   and promoted only when the attempt completes successfully.
3. **Run 2 (session B)** — a new event, hence a new session. The agent's
   workspace is initialized from the durable agent head only. It writes
   `out/report.txt`; you'll see the visible keys include
   `conclusions/latency` but not `scratch/measurements`, and
   `scratch-visible: no`. That file is the proof of scope isolation: the
   promoted conclusion travels, the scratch does not.
4. **Store inspection** — a script using Zeta's own store API decodes each
   content head: the session-A head holds the scratch, the agent head holds
   the conclusion, and run heads are per-run workspaces. (Note: content heads
   live in the *global* scope of `.zeta/zeta.sqlite3`; `zeta traces refs`
   shows per-session prompt refs, so the demo reads the heads directly.)
5. **Journaled promotions** — `zeta events list --type-prefix
   runtime.content.promoted` shows one durable event per promotion, with the
   old head, new head, and the agent's stated reason. Memory changes have
   provenance.
6. **Stale-overwrite protection (API-level demonstration)** — the agent tool
   path always reads the current head, so to show the guarantee directly the
   demo drives Zeta's own `ContentWorkspace.promote_all` — the same code path
   the coordinator runs at attempt success — from a script. Two simulated
   attempts read the same head H1; the first promotion is accepted and moves
   the head, the second (the older computation) is rejected with
   `ContentConflict`, and the final state is the newer conclusion. This part
   is not model-driven; it is labeled in the output as an API-level
   demonstration of the mechanism that protects every promotion.

One honest caveat: the model tasks are deliberately tiny and the agent is
told exactly which keys and scopes to use. The point is the runtime's commit
semantics, not the model's judgment about what deserves promotion.

## Where else this principle applies

- **Code-review or refactoring agents** that accumulate per-PR scratch
  analysis but should only durably remember confirmed conventions of the
  repository.
- **Customer-support agents** where per-ticket context must stay in the
  ticket's session while verified account facts get promoted to durable
  memory — and a stale ticket can't clobber a newer correction.
- **Ops/incident agents** that collect noisy observations during an incident
  and promote only the validated post-mortem conclusion, with the journal as
  the audit trail of who changed operational memory and why.
- **Self-improving procedures**: an agent that refines its own checklist
  (`kind: procedure`) over many runs, where concurrent runs can't silently
  drop each other's improvements thanks to head CAS.
- **Multi-agent pipelines** where fan-out workers stage candidate results and
  only the coordinator's success boundary decides what becomes shared truth.
