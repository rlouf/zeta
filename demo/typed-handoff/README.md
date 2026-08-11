# Typed handoff: agents hand off through schema-validated events

Two agents owned by two different teams cooperate without sharing a prompt, a
process, or a framework object. Team A's digest agent turns raw change entries
into a `release.summary.ready` event; Team B's announcer agent consumes that
event and writes the public announcement. The handoff contract is a JSON
Schema checked into `agents/events/release.summary.ready.json`, and the Zeta
runtime enforces it: when Team A's agent tries to publish a payload that
violates the schema, the runtime rejects the tool call with the exact
validation error before any event exists, and only a conforming payload
becomes a durable, causally-linked event in the log. The demo shows the
rejection, the validated event, and one `zeta events chain` that walks from
Team A's CI kickoff to Team B's completed run.

## Why other frameworks can't do this

- CrewAI and similar multi-agent frameworks pass prose between agents. The
  "contract" is whatever the upstream model happened to write; the downstream
  agent re-parses it and hopes. There is no artifact you can diff in code
  review that says what Team A owes Team B.
- LangGraph gives you typed state channels inside one graph, in one process,
  owned by one team. It does not give you a durable, schema-validated event
  a different team's agent can consume later, nor causality you can query
  after the fact.
- Temporal genuinely has typed payloads between activities (this half it can
  do), but the types live in each team's workflow code, not as a declarative
  schema resource next to the agent definitions, and Temporal has no concept
  of an agent loop whose *model output* is bounced back for repair when it
  violates the contract. Here the model received the validation error
  (`'the-world' is not one of ['customers', 'internal']`) and corrected
  itself in the next turn — that is visible in the event log.
- n8n validates node inputs ad hoc; there is no durable event log with
  `caused_by` provenance to debug across ownership boundaries.

## How to run

Prerequisites:

- Python 3.11+ and `uv`
- Codex CLI logged in (`codex login`) — the run makes **two live model
  calls** (one per agent), roughly 10–60 s each

```sh
./run.sh
```

Everything the script creates lives under `demo/typed-handoff/work/` and is
wiped at the start of each run. The script pins `ZETA_STATE_DIR` inside
`work/` so it never touches other runtime state in this repository.

## What you'll see and how to interpret it

1. **Scaffolding** — the project is three checked-in resources: two event
   schemas in `agents/events/` and two agent Markdown files. The schema file
   *is* the inter-team API.
2. **Publishing the kickoff event** — `release.notes.requested` enters the
   log the way Team A's CI would emit it.
3. **Team A's digest run** — one live model call. The agent is instructed to
   first attempt a deliberately malformed publish (`audience: "the-world"`)
   so you can watch the gate work, then publish correctly.
4. **The schema gate in action** — printed straight from the durable log:
   the REJECTED `publish_event` call with the runtime's validation error
   (`invalid-event-payload - 'the-world' is not one of ['customers',
   'internal']`), the ACCEPTED call, and the resulting typed
   `release.summary.ready` event with its validated payload and `caused_by`
   provenance. The malformed payload never became an event; the log contains
   only the conforming one.
5. **Causality across the boundary** — `zeta events chain` on the typed event
   walks back to the kickoff.
6. **Known runtime bug + bridge** — Team B's direct attempt fails with
   `ContentValidationError` (see "Two zeta bugs" below). The script
   re-publishes the byte-identical validated payload with `--caused-by`
   pointing at the agent's typed event, so causality is preserved.
7. **Team B's announcer run** — the second live model call; it writes
   `announcements/announcement.md` from the typed fields. The final
   `zeta events chain` shows one path: kickoff → Team A's run → typed event →
   bridge → Team B's completed run. Debugging follows causality, not Slack
   archaeology.
8. **Authoring-time rejection** — an agent declaring `publishes:` for an
   event type with no schema in `agents/events/` fails project load
   (`ManifestError: ... references unknown event ... in publishes`). You
   cannot even *declare* an unschema'd handoff.
9. **Honest edge** — `zeta events publish` with a schema-violating payload is
   **accepted**. See the next section for exactly where validation sits.

## Where validation sits today

Verified against the source, not the docs:

- `publish_event` tool calls made by an agent are validated against the
  schema from `agents/events/` before any event exists; violations return
  `invalid-event-payload` to the model (`zeta/src/zeta/tools/events.py`).
  This is the path the demo uses.
- `returns:` results are produced by a schema-constrained structured
  generation and validated again with a Draft 2020-12 validator before
  publication (`zeta/src/zeta/harness/returned_events.py`).
- Unknown event types in `accepts:`/`publishes:`/`returns:` are rejected at
  project load (`zeta/src/zeta/authoring/manifest.py`).
- `zeta events publish` (CLI) does **not** validate payloads — it is raw
  ingress into the log (`zeta/src/zeta/cli/commands/events.py`). Treat it as
  a trusted edge today.

## Two zeta bugs this demo works around

Claim only what runs, so here is what does not, as of this writing:

1. **`returns:` is unusable with the codex profile.** The demo was designed
   around `returns:` — the final no-tools structured generation validated
   against the event schema. `derive_returns_schema`
   (`zeta/src/zeta/authoring/returns.py`) wraps the return branches in a
   top-level `oneOf`, which the codex responses endpoint rejects with
   `400: 'oneOf' is not permitted`, failing every attempt. The demo therefore
   hands off through `publishes:` + the `publish_event` tool, which carries
   the same schema contract.
2. **Direct consumption of an agent-published event fails.** An event
   published via `publish_event` is tagged with the publishing run's `run_id`
   (`zeta/src/zeta/harness/dispatch.py`), the consumer's attempt adopts it
   (`zeta/src/zeta/harness/lifecycle.py`, `ids.run_id_for_attempt`), and the
   consumer's context store then hits
   `ContentValidationError: content revision belongs to another owner`
   (`zeta/src/zeta/context/transforms.py`). The run.sh bridge re-publishes
   the identical validated payload via the CLI with `--caused-by`, which
   clears the inherited `run_id` while preserving the causal chain. The
   announcer uses `retry: max_attempts: 1` so the doomed direct attempt
   dead-letters deterministically instead of retrying.

## Where else this principle applies

- **Service extraction from a monolith of prompts** — any place two teams'
  agents meet, the event schema is the reviewable, versionable API surface,
  like an OpenAPI spec between microservices.
- **Human-approval gates** — an `approval.granted` event with a schema-bound
  `scope` enum means an agent cannot fabricate a broader approval than the
  contract allows.
- **Data-pipeline handoffs** — an extraction agent that must emit
  `record.extracted` with typed fields cannot silently ship a
  differently-shaped record to the loading stage.
- **Incident response** — `incident.triaged` with a severity enum keeps a
  triage model from inventing severity levels the paging policy does not
  know.
- **Cross-organization integrations** — the schema file plus `caused_by`
  provenance gives both sides an auditable record of exactly what crossed
  the boundary and why.
