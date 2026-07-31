# Zeta Concepts And Reference

This is the full reference for Zeta. For the pitch and a quick start, see the
[README](../README.md).

## The Zeta Object Model

Zeta has four layers of concepts. Each layer answers a different question, and
each keeps its own identity rules.

| Layer | Question | Core types |
| --- | --- | --- |
| Authoring | What is an agent? | `AgentSpec`, event schema, skill, connector |
| Routing | What work exists? | `AgentDefinition`, `AgentRoute`, `EventPattern` |
| Runtime | What happened? | `Event`, `QueueItem`, `Attempt`, `Run`, session |
| Trace | What did the model see? | `Object`, `Derivation`, `Ref` |

The authoring layer is pure declaration. It knows nothing about queues, runs,
or SQLite. The runtime layer never reads the prompt. The trace layer never
decides what runs next. You can read any one layer without the other three.

### From File To Runtime Types

The runtime compiles one authored file into two separate shapes:

```text
agents/<slug>.md
  └─ AgentSpec ──compile──> AgentDefinition
                              ├─> AgentRoute      (does this event match?)
                              └─> ExecutableAgent (run the matched event)
```

The split is deliberate. `AgentRoute` decides whether an event belongs to an
agent. `ExecutableAgent` carries the code that runs it. Routing never needs the
runner, and the runner never needs the route table.

### The Runtime Chain

One inbound event becomes a chain of durable records:

```text
Event ──routing──> QueueItem ──claim──> Attempt ──> Run
(a fact)           (assignment)         (one try)   (handle)
```

- An **event** is a durable fact. It never changes.
- A **queue item** assigns one event to one agent. It holds routing state, not
  execution state.
- An **attempt** is one numbered try for a queue item. A retry adds an attempt.
  It never edits a terminal attempt.
- A **run** is the handle used to cancel work and report status.
- A **session** is the timeline scope. The agent declares what identifies it:
  itself, the triggering event, or a value the event carries.

Each arrow is one-to-many. One event can fan out to several queue items, and
one queue item can hold several attempts.

The `events` table is the only source of truth. `queue_items`, `attempts`,
`attempt_results`, and `session_mappings` are projections. You can delete them
and rebuild them from the journal. Claims, leases, and locks are neither facts
nor projections. They are live coordination state, and a rebuild discards them.
See [Runtime Semantics](runtime-semantics.md) for the state machines, the
derived id scheme, and the recovery rules.

### Effects Are Separate From Tool Calls

A tool call is a record of what the model asked for. An **effect** is the
separate concept for work that reaches outside Zeta, such as a file write or a
Slack message. An effect key identifies the logical operation independently of
the attempt number, so a retry recognises its own earlier work. Four delivery
contracts decide what a retry may do. See [Tools And Skills](#tools-and-skills).

### Traces Answer A Different Question

Runtime events say that a model call happened. The trace substrate says exactly
what the model saw. Every prompt component becomes an immutable `Object`,
addressed by its content hash. A `prompt` object links to its components and
stores only the hash of the request payload. A `Derivation` records which
producer built an object from which inputs.

This is why replay is sound. `zeta traces replay` rebuilds the prompt from the
stored components, then verifies it against the recorded hash before it sends
anything. See [Prompt And Tool Traces](#prompt-and-tool-traces) and
[the trace design note](zeta-prompt-trace.md).

### Where Things Live

The source tree states the ontology. Each package is one layer, and each layer
depends downward only.

```text
src/zeta/
  events.py       Event and DraftEvent, the core vocabulary
  effects.py      delivery contracts for external work
  ids.py          the derived id rules: queue item, attempt, run, session
  paths.py        the user configuration root

  substrate/      content-addressed trace objects, derivations, and refs
  journal/        the durable event log, its stores, and its record shapes
  trace/          query, render, diff, and replay over the substrate

  authoring/      AgentSpec, the manifest, skills, and event schemas
  context/        prompt assembly, budgeting, and compaction
  models/         model clients, profiles, endpoints, and streaming
  capabilities/   the tool registry, executors, and effect identity
  tools/          the built-in capability implementations

  loop/           one invocation: gateway, request, steps, orchestration
  harness/        routing, queue, attempts, runs, sessions, dispatch, worker
  cli/  rpc/      the interfaces

src/connectors/   ingress and egress, the third-party extension surface
```

Two rules hold the shape, and `test_import_boundaries.py` asserts both:

- `substrate`, `ids`, and `paths` are leaves. They import nothing from Zeta, so
  any layer may use them.
- `connectors` sees `zeta.events` and `zeta.effects` and nothing else, so an
  installed connector cannot reach into the runtime.

The harness and the loop meet at one seam. See
[Runtime Semantics](runtime-semantics.md#harness-and-agent-loop).

## Install

The `zeta-os` package installs the `zeta` command:

```sh
uv tool install zeta-os
zeta --help
```

For local development:

```sh
uv sync --group dev
uv run zeta --help
```

Zeta needs Python 3.11+ and Codex credentials. Run `codex login` once before
your first request. Zeta uses the Codex Responses backend by default.

You can instead configure an OpenAI-compatible Chat Completions endpoint:

```text
http://127.0.0.1:8080/v1/chat/completions
```

## Model Profiles

Model profiles live in `~/.zeta/models.toml`:

```toml
[[models]]
name = "fast"
model = "qwen2.5-coder"
url = "http://127.0.0.1:8080/v1/chat/completions"
thinking = "none"
default = true

[[models]]
name = "deep"
model = "qwen3-coder"
url = "http://127.0.0.1:8081/v1/chat/completions"
thinking = "high"
```

At most one profile may set `default = true`. If no default exists, Zeta uses
the built-in Codex profile: `codex` with `gpt-5.5`. `thinking` may be `"none"`,
`"minimal"`, `"low"`, `"medium"`, or `"high"`.

You can also make the Codex Responses backend an explicit profile:

```toml
[[models]]
name = "codex"
model = "gpt-5.5"
api = "codex-responses"
thinking = "high"
```

That sends prompts and any tool-read file contents to OpenAI's backend. Run
`codex login` first so `~/.codex/auth.json` exists.

The `zeta run` worker resolves its model from the project runtime session
stored under `.zeta/sessions/default`. For agents, the usual choices
are to set a `default = true` profile in `models.toml` or to set a per-agent
`model:` override in the agent frontmatter.

Inspect the configured profiles and the active selection:

```sh
zeta models list
zeta models show
```

## Project Layout

Zeta reads agent definitions from a flat `agents/` directory in the project root:

```text
agents/
  release-manager.md
  support-triage.md
  connectors.yaml
  events/
    github.pr.opened.json
    release.summary.ready.json
  skills/
    code-review.md
    release-notes.md
  tools/
```

Only top-level `agents/*.md` files are interpreted as agents. Directories such
as `agents/events/`, `agents/skills/`, and `agents/tools/` are resources, not
nested agents.

The filename stem is the agent slug. It must match `[a-z0-9_-]+`.

## Defining Agents

Each agent file is Markdown with YAML frontmatter and a Jinja prompt body. The
prompt body may reference one root variable, `event`; validation rejects other
undeclared roots.

```markdown
---
name: Slack Support
description: Replies to Slack support messages.
model:
  name: qwen3-coder
  url: http://127.0.0.1:8080/v1/chat/completions
accepts:
  - event: slack.message.received
    filter:
      channel_ids: ["C123"]
    idempotency_key: "slack:message:{team_id}:{channel_id}:{message_ts}"
returns:
  - event: slack.message.post
    with:
      channel_ids: ["C123"]
tools:
  - read
  - grep
skills:
  - code-review
schedules:
  - cron: "0 9 * * 1"
    timezone: Europe/Paris
    catchup: latest
session: shared
---
Reply to the Slack message:

{{ event.payload.text }}
```

Core frontmatter fields:

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Human-readable name. |
| `description` | yes | Used as the agent system prompt. |
| `enabled` | no | Defaults to `true`; disabled agents are ignored. |
| `session` | no | What identifies a session. `shared`, `per-event` (default), or a template. |
| `model` | no | Per-agent `{name, url}` override. |
| `accepts` | no | Event types that can trigger the agent. |
| `returns` | no | Event types the agent may publish after it finishes. |
| `tools` | no | Capability names granted to the model. |
| `skills` | no | Shared Markdown skills from `agents/skills/`. |
| `schedules` | no | Cron triggers that publish synthetic events. |
| `accepts[*].filter` | no | Connector-owned inbound event selection. |
| `accepts[*].idempotency_key` | no | Required for connector ingress bindings. |
| `returns[*].with` | no | Connector-owned delivery options for returned events. |
| `returns[*].idempotency_key` | no | Optional egress idempotency template. |

Schedules automatically add `agent.<slug>.scheduled` to `accepts`. For example,
`agents/release-manager.md` with a schedule accepts
`agent.release-manager.scheduled`.

By default, Zeta backfills a missed schedule only later on the same calendar
day. Set `catchup: latest` to keep the latest occurrence eligible across days.
Zeta records when that schedule is first observed, so it never catches up an
occurrence from before activation, and its idempotency key ensures the missed
occurrence is published only once.

### Session Scope

A session is the timeline scope, so it decides what an agent remembers. One
question decides it: what identifies a session? The `session` field answers it.

| `session` | Session id | Use it when |
| --- | --- | --- |
| `shared` | `agent/<slug>` | one timeline, across every event |
| `per-event` | `agent/<slug>/<event_id>` | unrelated events must not share timeline |
| a template | `agent/<slug>/<rendered>` | one timeline per conversation, customer, or repository |

`per-event` is the default.

A template names a field the event carries. It renders like an idempotency key,
so payload fields appear as top-level names and the event is available as
`event`:

```yaml
session: "{chat_id}"                  # one timeline per Telegram chat
session: "{channel_id}:{thread_ts}"   # one timeline per Slack thread
session: "{event.id}"                 # the same as per-event
```

A template that names a field the event lacks raises. It never falls back,
because a silent fallback would merge every conversation into one timeline.

### Scaffolding

`zeta new <path>` creates an empty project with the default Inbox Summarizer.
It creates the agent, its inbox, its summary folder, the filesystem connector,
and a `.gitignore` file for runtime state:

```sh
zeta new ~/zeta-demo
cd ~/zeta-demo
zeta serve
```

`zeta agents new <slug>` writes `agents/<slug>.md` from a template, validating it
before it is written. Options mirror the core fields:

```sh
zeta agents new note-filer \
  --name "Note Filer" \
  --description "Files new notes." \
  --accepts file.created \
  --tool read --tool write \
  --skill entity-matching \
  --base-dir ~/vaults/CEO
```

Use `--force` to overwrite an existing agent file.

## Events

Events are durable records with:

- `type`
- `source`
- object `payload`
- optional `idempotency_key`
- optional causality and runtime metadata

Project event schemas live under `agents/events/`. The JSON filename stem is
the event type:

```text
agents/events/github.pr.opened.json
```

The file may be a JSON Schema object directly:

```json
{
  "type": "object",
  "required": ["number", "title"],
  "properties": {
    "number": { "type": "integer" },
    "title": { "type": "string" }
  },
  "additionalProperties": false
}
```

or an object with a `schema` field:

```json
{
  "schema": {
    "type": "object",
    "additionalProperties": false
  }
}
```

External events listed in `accepts` and all events listed in `returns` must have
a schema from `agents/events/` or from an enabled connector. Scheduled events
are registered internally with an empty object payload schema.

## Returned Events

When an agent declares `returns`, Zeta runs the normal assistant/tool loop first.
After the loop finishes, it performs one final structured generation with no
tools available. The schema is derived from the declared return event schemas:

```json
{
  "events": [
    {
      "type": "release.summary.ready",
      "payload": {
        "summary": "Runtime release notes are ready."
      }
    },
    {
      "type": "release.summary.ready",
      "payload": {
        "summary": "SDK release notes are ready."
      }
    }
  ]
}
```

The `events` array may be empty and is capped at 100 items. Each item is
validated independently against its event schema, then published in order as a
durable event from `agent:<slug>`. Each position has a stable idempotency key,
so retries do not duplicate events that were already published. Connector
egress handlers can then deliver those events to external systems.

## Tools And Skills

The `zeta run` CLI registers the built-in Zeta capabilities with the local
runtime. Agent `tools:` entries can use either the bare model name or the
canonical capability id when it is unambiguous:

| Tool | Capability id | Purpose |
| --- | --- | --- |
| `read` | `zeta.read` | Read file contents. |
| `ls` | `zeta.ls` | List files. |
| `grep` | `zeta.grep` | Text search. |
| `ast_grep` | `zeta.ast_grep` | Structural code search. |
| `web_search` | `zeta.web_search` | Web search. |
| `bash` | `zeta.bash` | Run shell commands. |
| `edit` | `zeta.edit` | Edit files. |
| `write` | `zeta.write` | Write files. |

Capability execution goes through the registry. Zeta executes an allowed tool
when the model calls it. Zeta does not defer a tool call for later execution.

Side-effecting capabilities declare one delivery contract:

- `idempotent_with_key`: repeating the logical operation with the same effect
  key is safe.
- `connector_deduplicated`: the provider deduplicates the propagated key.
- `at_least_once`: retries are allowed and duplicate delivery is possible.
- `unsafe_to_retry`: an ambiguous started effect prevents automatic retry.

Side-effecting calls write `runtime.effect.*` records around execution.
The effect key is derived from the queue item, capability id, and canonical
arguments, so a retry receives the same identity even when the model tool-call
id changes. Built-in `write` and `edit` are content-idempotent; `bash` is unsafe
to retry automatically. Read-only capabilities do not create effect records.

Shared agent skills are Markdown files under `agents/skills/`. The filename stem
is the skill name, and agents opt in with `skills:`.

## Connectors

Connectors contribute event schemas, ingress, push ingress, egress, and filter
schemas. Installed connector packages are discovered through the
`zeta.event_connectors` entry point group, but a project must enable them in
`agents/connectors.yaml`:

```yaml
event_connectors:
  - slack
```

Connector-provided event schemas are merged with `agents/events/`. Duplicate
schemas must be identical.

Connector options live on the event entries they configure:

```yaml
accepts:
  - event: slack.message.received
    filter:
      channel_ids: ["C123"]
    idempotency_key: "slack:message:{team_id}:{channel_id}:{message_ts}"
returns:
  - event: slack.message.post
    with:
      channel_ids: ["C123"]
```

`accepts[*].filter` is validated against the connector's ingress selection
schema. Connector ingress bindings require `idempotency_key` so connectors can
avoid duplicate ingests. `returns[*].with` is validated against the connector's
egress options schema. Egress defaults to a connector/event idempotency key when
one is not supplied.

Each connector egress handler also declares delivery semantics in its connector
definition. Zeta records planned, started, completed, failed, or ambiguous
effect facts using the stable egress idempotency key. Retry-safe delivery
failures enter the normal queue retry policy. An unsafe ambiguous delivery is
dead-lettered for inspection instead of being sent again automatically.

The bundled Slack connector uses:

- `SLACK_BOT_TOKEN` for `chat.postMessage`
- `SLACK_SIGNING_SECRET` for push ingress request verification

The bundled Telegram connector uses:

- `TELEGRAM_BOT_TOKEN` for `sendMessage`
- `TELEGRAM_WEBHOOK_SECRET` for webhook request verification
- `TELEGRAM_ALLOWED_SENDERS` for a comma-separated user allowlist

Set the Telegram webhook to Zeta's `/connectors/telegram` endpoint. Include
`message` and `message_reaction` in the webhook `allowed_updates` list.
Telegram sends named user reactions only when the bot is a chat administrator.
Anonymous reactions have no user identity, so Zeta ignores them.

Use `telegram.message.reaction` when a reaction can confirm an action. The
event carries the reacted `message_id`, the user ID, and both reaction sets.
Match the message ID to the confirmation message before the agent acts.

Telegram also accepts voice, photo, document, and audio messages. Each received
message event can contain an `attachments` list with Telegram file metadata.
Zeta preserves the file ID and caption. It does not download or inspect media.

The bundled filesystem connector (`id: filesystem`) polls a directory and emits
`file.created` events with a `{path, name, dir}` payload.

## Runtime State

Zeta keeps project runtime state in `.zeta/`. This includes the SQLite database
for events, queue items, attempts, runs, and prompt traces, plus runtime session
state.

The runtime state directory is selected in this order:

1. an explicit `--state-dir`;
2. a non-empty `ZETA_STATE_DIR`;
3. the nearest existing `.zeta/` found by walking up from the command's starting
   directory;
4. `<start>/.zeta` if no marker exists.

Relative paths supplied by `--state-dir` or `ZETA_STATE_DIR` are resolved from
the directory where Zeta was invoked. Selecting the fallback does not create
it. A command that writes runtime state creates it only when needed; inspection
commands remain read-only and leave a project with no `.zeta/` unchanged.
Options belong to the leaf command that consumes them. For grouped resources,
put `--state-dir` after the operation, as in
`zeta events list --state-dir /path/to/state`.

Commands that only inspect runtime records start discovery from the current
directory. Commands that load agent definitions, including `schedules status`,
start from their resolved project root instead. `--project-root` therefore
belongs only to `run`, `serve`, `schedules status`, and `agents new`;
use `--state-dir` when a runtime-only inspection command must read a different
store.

Zeta does not treat `~/.zeta` as a project marker when walking up from a
directory below the home directory. That directory remains the user-level
configuration root, including `models.toml`, global skills, and the global
`AGENTS.md`. Project runtime discovery and user configuration are separate
concepts. If discovery starts at the home directory itself, the fallback is
naturally `~/.zeta`.

## Running Agents

Publish an event manually:

```sh
zeta events publish github.pr.opened \
  --payload-json '{"number":17,"title":"Fix release notes"}'
```

Fire due schedules and drain the queue until it is empty, then exit:

```sh
zeta run
```

Run the worker continuously instead:

```sh
zeta serve
```

Both `zeta run` and `zeta serve` fire due schedules before processing work.
Schedule publication belongs to workers; no separate scheduler process is
required.

`zeta serve` also serves push ingress. If enabled connectors expose it, the
worker listens for HTTP requests at:

```text
http://127.0.0.1:8080/connectors/<connector-id>
```

Change the host, port, route prefix, or connector allowlist:

```sh
zeta serve --host 0.0.0.0 --port 8090 --route-prefix /webhooks --connectors slack
```

Inspect schedule backfill and next-fire state without publishing anything:

```sh
zeta schedules status
```

## Observability And Debugging

The `zeta` CLI reads the discovered project runtime journal and queue. Listing
and inspection commands do not create or migrate runtime state. `events
publish` is the explicitly mutating command in this group:

```text
zeta queue status [--state-dir DIR]
zeta queue list [--state-dir DIR] [--json]
zeta attempts list [--state-dir DIR] [--json]
zeta ps [RUN_ID] [--state-dir DIR] [--json]
zeta events list [--state-dir DIR] [--type-prefix PREFIX] [--session ID] [--limit N] [--json]
zeta events chain EVENT_ID [--state-dir DIR] [--json]
zeta events root EVENT_ID [--state-dir DIR] [--json] [--raw]
zeta events descendants EVENT_ID [--state-dir DIR] [--json] [--raw]
zeta events turn TURN_ID [--state-dir DIR] [--json] [--raw]
zeta events publish EVENT_TYPE [--state-dir DIR] [--payload-json JSON] [--idempotency-key KEY]
zeta schedules status [--project-root DIR] [--state-dir DIR] [--json]
```

The causality commands walk the `caused_by` graph in both directions. `events
root` finds the original cause of an event, and `events descendants` finds
everything that event caused, recursively. `events turn` groups every event that
shares one `turn_id`. `--raw` needs `--json`.

Common flows:

```sh
# Is there work waiting, claimed, failed, or unhandled?
zeta queue status

# Inspect queued items.
zeta queue list
zeta queue list --json

# List run summaries newest in storage order.
zeta ps

# Inspect one run, including trigger event, queue item, attempt result,
# returned events, tool calls, and usage.
zeta ps run_att_qi_evt_123_issue-triage_1 --json

# Read raw durable events.
zeta events list --limit 100
zeta events list --type-prefix runtime.
zeta events list --session agent/issue-triage

# Follow causality from one event.
zeta events root evt_123
zeta events descendants evt_123
zeta events turn turn_456

# Publish a test event idempotently.
zeta events publish laptop.resumed \
  --payload-json '{"path":"heartbeat.txt"}' \
  --idempotency-key resume-1

# Check schedule backfill and next fire time.
zeta schedules status
```

Plain output is tab-separated for easy shell use. JSON output exposes the
underlying records, including queue item ids, attempt ids, run ids, event
payloads, token usage, final summaries, and errors.

Queue statuses are shown in this order when present:

```text
pending
available
claimed
completed
failed
cancelled
retry_scheduled
unhandled
```

Runtime events use the following prefixes:

- `runtime.queue_item.*` for routing and queue lifecycle
- `runtime.attempt.*` for worker attempts
- `runtime.egress.*` for connector delivery
- `zeta.*` for model, prompt, tool, and turn records
- `rpc.*` for event-log JSON-RPC work

## Prompt And Tool Traces

Runtime events answer "what happened?" Prompt traces answer "what exactly did
the model see?" They are stored in the discovered project runtime database,
scoped by session id. Listing and inspecting traces are read-only; `traces
replay` records its replay. `traces reinit-store` is the destructive exception:
it deletes every trace and runtime record in the selected unified database.

```sh
# List recent prompts and assistant messages across agent sessions.
zeta traces log --all-sessions

# List failed or successful tool calls.
zeta traces tools --failed --all-sessions
zeta traces tools --successful --json --all-sessions

# Inspect one agent session.
zeta traces log --session agent/issue-triage
zeta traces show 4f9d01c2 --session agent/issue-triage
zeta traces tree 4f9d01c2 --session agent/issue-triage --down

# Search stored object payloads, optionally by kind.
zeta traces grep "release notes" --kind prompt --limit 20
zeta traces grep TODO --all-sessions

# Inspect the object graph and the store itself.
zeta traces closure 4f9d01c2 --session agent/issue-triage
zeta traces refs --session agent/issue-triage
zeta traces prompts --session agent/issue-triage

# Compare two prompts component by component.
zeta traces diff A B --session agent/issue-triage --stat

# Rebuild and resend a stored prompt.
zeta traces replay PROMPT_ID --session agent/issue-triage --model fast --diff
```

`traces grep` searches stored object payloads and accepts a repeatable `--kind`
filter. `traces closure` lists every object reachable from one object, which
shows the full component set behind a prompt. `traces refs` lists the mutable
refs and their targets, such as `prompt/current`. `traces prompts` lists
recorded prompts with store size statistics.

Every trace id argument accepts a full id, a unique prefix, or a ref such as
`turn/<turn_id>`. `traces replay` verifies the rebuilt prompt payload against the
recorded hash before sending it to the selected model. Trace scope options
belong to each leaf: put `--state-dir` and `--session` after `log`, `show`,
`replay`, or another operation. `traces reinit-store` is database-wide and
accepts only `--state-dir`.

A worked walkthrough lives in
[demos/trace-replay.md](demos/trace-replay.md).

## JSON-RPC

`zeta rpc stdio` serves newline-delimited JSON-RPC 2.0. Each line is one JSON
object. Requests include `jsonrpc: "2.0"`, an `id`, a `method`, and optional
object `params`; notifications omit `id`.

Supported methods:

| Method | Purpose |
| --- | --- |
| `initialize` | Return server and protocol metadata. |
| `session.run` | Start a session run. |
| `session.cancel` | Cancel an active run by `run_id`. |
| `events.list` | List durable events by cursor, session, turn, and limit. |
| `events.publish` | Append a client-authored durable event. |
| `tools.register` | Register client-hosted capabilities. |
| `tools.respond` | Respond to a `tools.call` notification. |

`session.run` accepts an optional non-empty `idempotency_key`. Reuse that key
only when retrying the same logical request. Zeta scopes the key to the
session, so a retry never starts a second run or repeats its effects. While the
original run is still active, the retry returns its existing started event;
after it finishes, the retry returns the original terminal result.

Server notifications:

| Notification | Purpose |
| --- | --- |
| `events.notify` | Carries a persisted runtime event. |
| `tools.call` | Asks the client to execute a registered capability. |

Protocol `0.1` is additive. Clients should ignore unknown result fields and
unknown notification params.
