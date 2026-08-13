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
`attempt_results`, `deferred_publications`, `waits`, and `session_mappings` are
projections. You can delete them and rebuild them from the journal. Claims,
leases, and locks are neither facts nor projections. They are live coordination
state, and a rebuild discards them.
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
  addresses.py    b3: content addresses, one frozen context per domain
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
  cli/  ipc/      local command and process interfaces

src/connectors/   connector bindings and self-described manifest vocabulary
```

Two rules hold the shape, and `test_import_boundaries.py` asserts both:

- `substrate`, `addresses`, `ids`, and `paths` are leaves. They import only
  the standard library, the blake3 address hash, and each other, so any layer
  may use them.
- `connectors` holds the small vocabulary used to validate connector manifests.
  Installed connector code runs in a child process and communicates through
  IPC.

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
the built-in Codex profile: `codex` with `gpt-5.6-sol`. `thinking` may be `"none"`,
`"minimal"`, `"low"`, `"medium"`, or `"high"`.

You can also make the Codex Responses backend an explicit profile:

```toml
[[models]]
name = "codex"
model = "gpt-5.6-sol"
api = "codex-responses"
tool_profile = "codex"
thinking = "high"
```

That sends prompts and any tool-read file contents to OpenAI's backend. Run
`codex login` first so `~/.codex/auth.json` exists.

`tool_profile` selects the tool names, descriptions, and input schemas that the
model sees. If you omit it, Zeta uses `native`. The built-in Codex model uses
`codex`. Zeta does not infer the tool profile from the model name. The Codex
profile presents `zeta.bash` as `exec_command` and `zeta.patch` as
`apply_patch`. Other capabilities keep their native presentation.

The `zeta run` worker resolves its model from the project runtime session
stored under `.zeta/sessions/default`. For agents, the usual choices
are to set a `default = true` profile in `models.toml` or to set a per-agent
`model:` override in the agent frontmatter.

See [Model-Specific Tool Profiles](tool-profiles.md) for the design, the
execution flow, and the `zeta.edit` and `zeta.patch` contracts.

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
  events/
    github.pr.opened.json
    release.summary.ready.json
  skills/
    code-review.md
    release-notes.md
  tools/
    release-manager/
      check_release.py
```

Only top-level `agents/*.md` files are interpreted as agents. Directories such
as `agents/events/`, `agents/skills/`, and `agents/tools/` are resources, not
nested agents.

A Python file in `agents/tools/<agent-slug>/` defines one tool for that agent.
The file must expose one `RegisteredCapability` as `tool` or through a
zero-argument `tool()` factory. Zeta validates the file and records its exact
source in the project generation. See [Zeta Tools](zeta-tools.md#agent-authored-tools).

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
publishes:
  - event: slack.message.post
    with:
      channel_ids: ["C123"]
returns:
  - support.reply.completed
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
| `publishes` | no | Event types that the agent can publish as intermediate effects. |
| `returns` | no | Event types eligible for the one final typed result of a turn. |
| `tools` | no | Capability names granted to the model. Omit this field to grant all available tools. |
| `skills` | no | Shared Markdown skills from `agents/skills/`. Omit this field to grant all available skills. |
| `schedules` | no | Cron triggers that publish synthetic events. |
| `accepts[*].filter` | no | Connector-owned inbound event selection. |
| `accepts[*].idempotency_key` | no | Required for connector ingress bindings. |
| `publishes[*].with` | no | Connector-owned delivery options for published events. |
| `publishes[*].idempotency_key` | no | Optional egress idempotency template. |

Schedules automatically add `agent.<slug>.scheduled` to `accepts`. For example,
`agents/release-manager.md` with a schedule accepts
`agent.release-manager.scheduled`.

Each scheduled event carries the intended occurrence in the schedule's
timezone:

```json
{
  "date": "2026-08-09",
  "timestamp": "2026-08-09T07:00:00-06:00"
}
```

These values describe the cron occurrence, including backfills and catch-up,
not the later time when a worker processes it. Schedules without a timezone
use UTC. This also makes the occurrence available to session templates:

```yaml
session: "morning-briefing-{date}"
```

Zeta applies the same selection rule to `tools` and `skills`:

- Omit the field to select all available entries.
- Use `[]` to select no entries.
- List names to select only those entries.

Zeta resolves omitted fields when it creates the project generation. The
generation records the exact tool and skill names. A later catalog change does
not change an existing generation.

### Published Effects and Returned Results

`publishes` and `returns` are independent declarations. `publishes` grants the
`publish_event` tool during the normal agent/tool loop. In the Slack Support
example, the agent can publish `slack.message.post` as an intermediate effect
while it works. `returns` does not grant a tool. After a successful normal
loop, Zeta runs one final no-tools structured generation that must select one
declared return type, here `support.reply.completed`, and produce a payload
valid against its registered event schema. Zeta persists and routes that event
with the agent and triggering-event provenance. An agent may use both fields:
send the Slack reply through `publishes`, then return the typed resolution
through `returns`.

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
session: "morning-briefing-{date}"    # one timeline per scheduled date
```

A template that names a field the event lacks raises. It never falls back,
because a silent fallback would merge every conversation into one timeline.

### The Master Agent

Zeta includes one authored agent named `zeta.master`. It provides a neutral
entry point when a user starts a session without naming an agent. Its omitted
`tools` and `skills` fields give it the complete catalog for the project
generation.

The master uses the same authored-agent compiler and runtime as project agents.
It does not have a separate execution path. A project agent can also own a
user-started session when the user names that agent.

A user can send more messages to an existing session. Each message becomes a
durable event and a directly bound queue item for the session owner. The
message keeps the existing session id, so the agent receives the existing
timeline.

Zeta runs one turn at a time in each session. It uses the durable input order.
A retry keeps its position and blocks later turns in the same session. Other
sessions can run at the same time.

### Scaffolding

`zeta new <path>` creates an empty project with the default Inbox Summarizer.
It creates the agent, its inbox, its summary folder, and a `.gitignore` file
for runtime state. The installed filesystem connector registers automatically:

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

External events listed in `accepts` and all events listed in `publishes` must
have a schema from `agents/events/` or from an enabled connector. Scheduled
events are registered internally with an empty object payload schema.

## Publishing Events

When an agent declares `publishes`, Zeta gives the agent the `publish_event` control
tool. The tool does not belong in the authored `tools` list. The model can use
`publish_event` more than once during the run:

```json
{
  "event_type": "release.summary.ready",
  "payload": {
    "summary": "Runtime release notes are ready."
  }
}
```

The optional `at` field schedules the event for an absolute future time:

```json
{
  "event_type": "release.summary.ready",
  "payload": {
    "summary": "Runtime release notes are ready."
  },
  "at": "2030-01-02T09:00:00+01:00"
}
```

Zeta checks the event type, payload schema, and time. It then returns a stable
publication handle. It keeps valid requests until the attempt succeeds. A
failed, cancelled, or stale attempt publishes nothing.

The model must call `publish_event` for each event. Zeta does not infer events
from the model response. Zeta publishes all requested events in call order
after the attempt completes. Connector egress handlers can then deliver these
events to external systems.

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
| `query_log` | `zeta.query_log` | Query prior runs in the current session. |
| `query_context_budget` | `zeta.query_context_budget` | Inspect the active model context budget. |
| `bash` | `zeta.bash` | Run shell commands. |
| `edit` | `zeta.edit` | Edit files. |
| `patch` | `zeta.patch` | Apply patches to files. |
| `write` | `zeta.write` | Write files. |

`publish_event` is a runtime control tool, not an installed capability. It is
available only when the agent declares at least one event in `publishes`. It
does not use the selected tool executor and does not create capability effect
records. See [Zeta Tools](zeta-tools.md#publish_event) for the complete tool
contract.

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

`query_log` reads prior model runs from the durable journal. Its runtime-bound
reader always scopes the query to the current session and excludes the active
run. Models can list recent runs, filter by time or failed outcome, and expand
one run by id prefix. Listing and expansion are bounded; models cannot select
another session through tool arguments. Expansion includes the objective,
outcome, final answer, compact tool activity, and prompt trace ids; it does not
replay provider usage telemetry or raw tool arguments and results.
See [Zeta Tools](zeta-tools.md#query_log) for the complete tool contract.

`query_context_budget` reports the latest prompt use, the model context window,
the output reservation, and the remaining token budget. It also reports the
active prompt compaction strategy and threshold. The tool does not change the
prompt. A model can use the result to decide when it should reduce content.
See [Zeta Tools](zeta-tools.md#query_context_budget) for the complete tool
contract.

Shared agent skills are Markdown files under `agents/skills/`. The filename stem
is the skill name, and agents opt in with `skills:`.

## Connectors

A connector is a child program that speaks the IPC protocol in
`spec/ipc-v0.md`. A Python package registers one under the
`zeta.event_connectors` entry-point group. The entry-point name is the
connector id, and its target starts the connector. Installing the package in
Zeta's active Python environment registers it. For example:

```sh
uv pip install zeta-connectors
```

The Python frontend reads the installed metadata and turns each registration
into an explicit launch spec containing the connector id and argument vector.
It passes those launch specs to the runtime without importing connector code.
The runtime reads each child's `--describe` manifest for its event schemas,
filter schemas, and delivery semantics, then supervises its IPC process. A
connector whose describe step fails is skipped with a warning instead of
failing an unrelated project. `zeta serve --connectors` narrows one runtime
process to an explicit allowlist.

Another ecosystem can use its native registration system in its frontend and
produce the same id-and-argument-vector launch specs. The runtime behavior and
IPC protocol stay the same.

Connector-provided event schemas are merged with `agents/events/`. Duplicate
schemas must be identical.

Ingress runs as supervised subprocesses. The runtime groups bindings that
share an upstream cursor, such as Telegram bindings, into one child. That child
initializes with the `source` role and calls `events.publish`. A successful
response means the event was durably inserted or resolved as a duplicate, so
the connector may advance its upstream cursor. The supervisor restarts dead
children with capped backoff; connectors hold no reconnect loop of their own.
For egress, the runtime calls the direct methods declared by a child with the
`provider` role. Non-secret settings arrive in the initialization `config`;
secrets travel only in the spawn environment.

Connector options live on the event entries they configure:

```yaml
accepts:
  - event: slack.message.received
    filter:
      channel_ids: ["C123"]
    idempotency_key: "slack:message:{team_id}:{channel_id}:{message_ts}"
publishes:
  - event: slack.message.post
    with:
      channel_ids: ["C123"]
```

`accepts[*].filter` is validated against the connector's ingress selection
schema. Connector ingress bindings require `idempotency_key` so connectors can
avoid duplicate ingests. `publishes[*].with` is validated against the connector's
egress options schema. Egress defaults to a connector/event idempotency key when
one is not supplied.

Each connector operation declares delivery semantics in its describe
manifest. Zeta records planned, started, completed, failed, or ambiguous
effect facts using the stable egress idempotency key. Retry-safe delivery
failures enter the normal queue retry policy. An unsafe ambiguous delivery is
dead-lettered for inspection instead of being sent again automatically.

The bundled Slack connector ingests over Socket Mode and uses:

- `SLACK_APP_TOKEN` (xapp-) for the Socket Mode websocket
- `SLACK_BOT_TOKEN` (xoxb-) for `chat.postMessage`

The bundled Telegram connector long-polls `getUpdates` — no webhook, no
public endpoint — and confirms offsets only after Zeta journals the event, so
nothing is lost between Telegram and the journal. It uses:

- `TELEGRAM_BOT_TOKEN` for polling, `sendMessage`, and media download
- `TELEGRAM_ALLOWED_SENDERS` for a comma-separated user allowlist
- `TELEGRAM_MEDIA_DIR` for an optional private media cache path
- `TELEGRAM_MEDIA_MAX_BYTES` for an optional media size limit

Telegram sends named user reactions only when the bot is a chat administrator.
Anonymous reactions have no user identity, so Zeta ignores them.

Use `telegram.message.reaction` when a reaction can confirm an action. The
event carries the reacted `message_id`, the user ID, and both reaction sets.
Match the message ID to the confirmation message before the agent acts.

Telegram also accepts voice, photo, document, and audio messages. Each received
message event contains an `attachments` list with local file paths and Telegram
metadata. Zeta stores each attachment under `.zeta/media/telegram` by default.
It preserves the file ID, caption, and MIME type. The default size limit is 20 MiB.
Zeta returns a retryable failure when a media download fails.

### Pushover

The bundled Pushover connector sends notifications to the Pushover app. The
app can show a notification on an iPhone and a paired Apple Watch.

The connector registers automatically once `zeta-connectors` is
installed. Set these environment variables:

- `PUSHOVER_API_TOKEN` is the application API token.
- `PUSHOVER_USER_KEY` is the user or group key.
- `PUSHOVER_DEVICE` is an optional device name.

Pushover can send to all active devices when a device name is invalid. Do not
use `PUSHOVER_DEVICE` as a security control.

Declare each event that an agent can publish:

```yaml
publishes:
  - event: pushover.message.send
  - event: pushover.glance.update
```

Use `pushover.message.send` for an alert that needs attention. For example, an
agent can report a failed deployment, ask for approval, or confirm that a long
job is complete:

```json
{
  "event_type": "pushover.message.send",
  "payload": {
    "title": "Deployment failed",
    "message": "The production health check did not pass.",
    "priority": 1,
    "url": "https://example.com/runs/42",
    "url_title": "Open run"
  }
}
```

The `message` field is required. A message can also contain `title`, `priority`,
`sound`, `url`, `url_title`, `ttl`, `retry`, and `expire`. Priority `2` is an
emergency notification. It requires `retry` and `expire`. Pushover has no
idempotency key for messages. Zeta can send a duplicate after an uncertain
delivery failure.

Use `pushover.glance.update` for short status data that is useful without
opening an app. For example, a Glance can show a queue count, job progress, or
the current service state:

```json
{
  "event_type": "pushover.glance.update",
  "payload": {
    "title": "Release",
    "text": "Uploading artifacts",
    "count": 3,
    "percent": 60
  }
}
```

A Glance update can contain `title`, `text`, `subtext`, `count`, or `percent`.
The update must contain at least one field. An omitted field keeps its current
value. An empty string clears a field. Zero is a valid count or percent.

Use an hourly schedule for regular Glance updates. Pushover recommends at
least 20 minutes between updates and permits no more than 50 Apple Watch
updates per day. Zeta does not retry an uncertain Glance delivery. A retry
could use more of this daily allowance.

The Apple Watch must have a registered Pushover Glance or complication. A
change can take up to ten minutes to appear.

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
belongs only to `run`, `serve`, `schedules status`, `sessions start`,
`sessions send`, and `agents new`;
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

Inspect or cancel deferred publications requested by agents:

```sh
zeta events deferred
zeta events cancel-deferred pub_0123456789abcdef
```

Fire due schedules and drain the queue until it is empty, then exit:

```sh
zeta run
```

Run the worker continuously instead:

```sh
zeta serve
```

Both `zeta run` and `zeta serve` ask the scheduler to emit due occurrences
before processing work. The scheduler owns cron, timezone, and catch-up policy;
Dispatch sees the resulting `agent.<slug>.scheduled` event as ordinary ingress.
The scheduler runs inside the worker, so no separate scheduler process is
required.

The native Rust host exposes the same operation as `Scheduler::tick`, compiled
from a verified project manifest and backed by durable
`zeta.scheduler.tick.*` facts. The native worker loop is not implemented yet;
when it is, it must call this operation at the same point in each pass. Calendar
policy stays in the host rather than moving into Dispatch.

Narrow the connector allowlist for one runtime process:

```sh
zeta serve --connectors slack
```

Inspect schedule backfill and next-fire state without publishing anything:

```sh
zeta schedules status
```

Start a session with the packaged master agent:

```sh
zeta sessions start "Plan the next release"
```

The command stores the message and returns immediately. A `zeta run` or
`zeta serve` worker processes it. Use the returned session id to send another
message or inspect activity:

```sh
zeta sessions send session_123 "Include the migration"
zeta sessions status session_123
zeta sessions list
```

`sessions send` keeps the existing owner agent. It uses that agent's current
definition and project generation. It fails if the owner is not enabled in the
current project. Use `--idempotency-key` when a client may repeat a start or
send request.

## Observability And Debugging

The `zeta` CLI reads the discovered project runtime journal and queue. Listing
and inspection commands do not create or migrate runtime state. `events
publish`, `sessions start`, and `sessions send` create durable work:

```text
zeta queue status [--state-dir DIR]
zeta queue list [--state-dir DIR] [--json]
zeta attempts list [--state-dir DIR] [--json]
zeta sessions start MESSAGE [--project-root DIR] [--state-dir DIR] [--idempotency-key KEY] [--json]
zeta sessions send SESSION_ID MESSAGE [--project-root DIR] [--state-dir DIR] [--idempotency-key KEY] [--json]
zeta sessions status SESSION_ID [--state-dir DIR] [--json]
zeta sessions list [--state-dir DIR] [--json]
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
# published events, tool calls, and usage.
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

## IPC

Zeta uses the newline-delimited JSON-RPC 2.0 profile in
[`spec/ipc-v0.md`](../spec/ipc-v0.md) for every process boundary. Each peer
must call `initialize` first and declare its role. `zeta ipc stdio` opens the
`client` endpoint used by the terminal interface.

The fixed methods are:

| Method | Role and purpose |
| --- | --- |
| `initialize` | Establish the peer, roles, limits, and protocol version. |
| `events.publish` | A `source` durably publishes one event. |
| `events.list` | A `client` lists durable events by cursor and scope. |
| `event` | The runtime notifies a `client` about one committed event. |
| `session.start` | A `client` queues a new session with the packaged master agent. |
| `session.send` | A `client` queues a message for an existing session owner. |
| `session.status` | A `client` reads one durable session activity record. |
| `session.list` | A `client` reads the durable session catalog. |
| `session.cancel` | A `client` records cancellation by `run_id`. |
| `ping` | Either initialized side proves liveness. |
| `shutdown` | A supervising process requests an orderly stop. |

A provider declares direct method names during initialization. The runtime
calls those names without an operation wrapper.

`session.start` and `session.send` return `queued` after Zeta stores the queue
binding. They do not run the model and do not wait for a worker. Both methods
accept an optional non-empty `idempotency_key`.

`session.cancel` accepts `run_id` and optional `session_id` and `reason`
fields. The session ID checks ownership when a caller supplies it. The request
is durable. It can cancel a run created by `session.start` or `session.send`,
even after the first IPC client disconnects.

The `event` notification is best effort and has no acknowledgment. Its cursor
lets a client ignore duplicates, detect gaps, and recover with `events.list`.
