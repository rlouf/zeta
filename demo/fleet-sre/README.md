# Fleet SRE: an SRE agent for the agents

A small fleet of Zeta agents processes events. One agent works, one fails
repeatedly, and one event has no handler at all. Then a meta-agent — the Fleet
SRE — diagnoses all of it and files a typed, schema-validated
`fleet.incident.report` event. The point: in Zeta, queue items, attempts,
retries, and events are durable state machines in a single SQLite journal, so
fleet health is a *query over runtime state*, not instrumentation bolted onto
workers. The diagnosis itself becomes a run and an event in the same journal,
so the observability is as auditable as the work it observes.

## Why other frameworks can't do this

- **LangGraph / CrewAI**: agent state lives in the orchestrating process. When
  a worker dies or a callback is dropped, the state goes with it. To answer
  "what failed and how many times?" you must have wired logging or tracing in
  advance, and that telemetry lives outside the execution model — it is not
  the thing that decides retries or dead-lettering, only a shadow of it.
- **Temporal**: has genuinely durable histories and can answer "what failed"
  per workflow — this is the half it does. But there is no first-class notion
  of an *unhandled* event (a signal to a workflow that doesn't exist is an
  error at the client, not a durable queue state), and a diagnosing workflow
  querying the whole fleet means going through Temporal's visibility APIs,
  which are indexed metadata, not the event-sourced record itself.
- **n8n**: execution logs exist per workflow run, but they are logs — you
  cannot derive the current queue/attempt state machine from them, and a
  failed webhook with no listening workflow leaves no durable trace at all.

In Zeta the tabular output of `zeta queue list` is not a report about the
runtime, it *is* the runtime: the same rows fence claims, drive retries, and
block or admit new work. Anything that can run a query — including another
agent — has complete observability for free.

## How to run

Prerequisites:

- Python 3.11+ and `uv`
- Codex CLI logged in (`codex login`) — the demo makes **live model calls**
  (one for the healthy worker, one short multi-tool run for the SRE agent;
  the failing worker fails before any model call)

```sh
./run.sh
```

Everything is created under `work/` (wiped at each start) and uses an isolated
state dir (`work/.zeta`), so it cannot touch other projects. One full run takes
about half a minute. Agents use the built-in `codex` model profile; the demo
deliberately passes no `--model` flag.

## What you'll see and how to interpret it

1. **Scaffolding** — three agents are generated from templates: `greeter`
   (healthy), `metrics-reporter` (its prompt template indexes
   `event.payload.rows[0]`, and declares `retry: max_attempts: 2`), and
   `fleet-sre` (the meta-agent).
2. **Publishing fleet traffic** — three events: a well-formed `ops.ping`; an
   `ops.metrics.report` *without* its `rows` field, so the reporter's prompt
   render raises on every attempt; and `ops.telemetry.flush`, which no agent
   accepts. Then `zeta run` drains the queue.
3. **Raw fleet state** — the failure states, straight from the journal:
   - `zeta queue list`: `completed` (greeter), `dead_lettered`
     (metrics-reporter, after its retries were exhausted), `unhandled` (the
     telemetry event — an event nobody accepts becomes a durable terminal
     queue state, not a dropped message).
   - `zeta attempts list`: **two** `failed` attempts for the same queue item —
     the retry state machine ran attempt 2 and only then dead-lettered.
   - `zeta ps`: the failed runs carry the exact error
     (`TemplateError: ... 'dict object' has no attribute 'rows'`).
   - `zeta events list --type-prefix runtime.queue_item.`: the append-only
     lifecycle facts (`claimed`, `available`, `dead_lettered`, `unhandled`)
     that the tables above are projections of.
4. **The SRE run** — a `fleet.check.requested` event triggers the `fleet-sre`
   agent. Its instructions have it run three read-only CLI queries
   (`queue list`, `attempts list`, `events list`) through its `bash` tool —
   the *same* queries you just saw — count the failure states, and call
   `publish_event` with a `fleet.incident.report`. The payload is validated
   against `agents/events/fleet.incident.report.json` before the event is
   accepted; the event is only committed because the SRE attempt completed
   successfully (requested events are written inside the attempt's completion
   transaction).
5. **The report on the record** — the printed payload contains the counts
   (2 failed attempts, 1 dead-lettered item, 1 unhandled event) and a summary
   citing the queue item ids. `zeta ps` now shows the diagnosis as a completed
   run of `fleet-sre`, and `zeta events chain` walks from the health-check
   request to the report. The report event itself enters routing and — since
   no agent subscribes to it — parks as one more `unhandled` queue item: even
   the absence of a subscriber is on the record.

One honest caveat about tooling: Zeta's built-in history tool (`query_log`) is
deliberately scoped to the calling agent's *own session* — it cannot see other
agents' runs. That is why the SRE agent queries the fleet through the CLI (via
its `bash` tool) instead. Fleet-wide state is still a plain query over the
journal; it is just the operator query surface, not the session-history tool,
that exposes it.

## Where else this principle applies

- **Billing and quota enforcement**: metering model calls per agent is a
  query over attempts and traces, not a metrics pipeline to keep in sync.
- **Compliance and audit**: "which agent acted on this customer event, with
  what payload, and what did it cause" is `zeta events chain` — the audit
  trail is the execution substrate, so it cannot drift from reality.
- **Postmortems for autonomous pipelines**: a data or CI pipeline built on
  durable queue/attempt machines can be diagnosed after the fact from the
  journal alone, even if every worker process is gone.
- **Self-healing fleets**: the next step after this demo — an agent that
  accepts `fleet.incident.report` and republishes corrected payloads or
  disables a crash-looping agent, closing the loop entirely inside the
  journal.
- **Capacity planning**: queue lag and retry pressure are aggregations over
  the same tables (`runtime.queue_lag_ms`, `runtime.retries_scheduled` in the
  metrics sink), not a separate observability stack to deploy.
