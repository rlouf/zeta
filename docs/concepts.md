# Zeta Concepts And Reference

This is the full reference for Zeta. For the pitch and a quick start, see the
[README](../README.md).

## Install

The Python packages are published as `zeta-os` and `commas`. `zeta-os` installs
the `zeta` command; `commas` installs the shell frontend:

```sh
uv tool install zeta-os
uv tool install commas
zeta --help
```

For local development:

```sh
uv sync --group dev
uv run zeta --help
uv run commas --help
```

Zeta needs Python 3.11+ and a model endpoint. By default, local runs use an
OpenAI-compatible chat completions endpoint at:

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

At most one profile may set `default = true`. If no default exists, Zeta falls
back to `local-model` at the default local URL. `thinking` may be `"none"`,
`"minimal"`, `"low"`, `"medium"`, or `"high"`.

A profile can also use the Codex Responses backend through local Codex CLI
credentials:

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

The Commas shell frontend also uses these profiles, but its `commas model use`
command selects a profile for the current shell session, not for a project
worker:

```sh
zeta model list
commas model use fast
zeta model show
commas model clear
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
resumable: true
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
| `resumable` | no | Reuse `agent/<slug>` session state across events. |
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

When `resumable: true`, every event for the agent uses session
`agent/<slug>`. Otherwise each event uses `agent/<slug>/<event_id>` so unrelated
events do not share timeline.

### Scaffolding

`zeta agent new <slug>` writes `agents/<slug>.md` from a template, validating it
before it is written. Options mirror the core fields:

```sh
zeta agent new note-filer \
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
  "type": "release.summary.ready",
  "payload": {
    "summary": "Release notes are ready."
  }
}
```

The validated result is published as a durable event from `agent:<slug>`.
Connector egress handlers can then deliver those events to external systems.

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
| `query_log` | `zeta.query_log` | Query Zeta history. |
| `bash` | `zeta.bash` | Run shell commands. |
| `edit` | `zeta.edit` | Edit files. |
| `write` | `zeta.write` | Write files. |

Capability execution goes through the registry and each capability's policy. In
the local worker, mutating tools use the current staged execution contract unless
the host supplies a different runtime configuration.

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

The bundled Slack connector uses:

- `SLACK_BOT_TOKEN` for `chat.postMessage`
- `SLACK_SIGNING_SECRET` for push ingress request verification

The bundled filesystem connector (`id: filesystem`) polls a directory and emits
`file.created` events with a `{path, name, dir}` payload.

## Running Agents

Zeta stores runtime state under `~/.zeta/` by default. Override it with
`--state-dir` when needed.

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

Both `zeta run` and `zeta serve` fire due schedules before processing work, so
the worker alone is enough to drive scheduled agents. The standalone
`zeta schedule` service below is only needed when you want to run the scheduler
in a separate process.

`zeta serve` also serves push ingress. If enabled connectors expose it, the
worker listens for HTTP requests at:

```text
http://127.0.0.1:8080/connectors/<connector-id>
```

Change the host, port, route prefix, or connector allowlist:

```sh
zeta serve --host 0.0.0.0 --port 8090 --route-prefix /webhooks --connectors slack
```

Run the scheduler once:

```sh
zeta schedule --once
```

Run the scheduler continuously:

```sh
zeta schedule
```

In continuous mode, the scheduler checks once per minute and publishes due
synthetic events such as `agent.release-manager.scheduled`.

## Observability And Debugging

The `zeta` CLI reads the project runtime journal and queue:

```text
zeta status
zeta queue [--json]
zeta attempts [--json]
zeta runs [--json]
zeta run show RUN_ID [--json]
zeta events [--type-prefix PREFIX] [--session ID] [--limit N] [--json]
zeta events publish EVENT_TYPE [--payload-json JSON] [--idempotency-key KEY]
zeta schedule status [--json]
```

Common flows:

```sh
# Is there work waiting, claimed, failed, or unhandled?
zeta status

# Inspect queued items.
zeta queue
zeta queue --json

# List run summaries newest in storage order.
zeta runs

# Inspect one run, including trigger event, queue item, attempt result,
# returned events, tool calls, and usage.
zeta run show run_att_qi_evt_123_issue-triage_1 --json

# Read raw durable events.
zeta events --limit 100
zeta events --type-prefix runtime.
zeta events --session agent/issue-triage

# Publish a test event idempotently.
zeta events publish laptop.resumed \
  --payload-json '{"path":"heartbeat.txt"}' \
  --idempotency-key resume-1

# Check schedule backfill and next fire time.
zeta schedule status
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
the model see?" They are stored in `~/.zeta/zeta.sqlite3`, scoped by session id.

```sh
# List recent prompts and assistant messages across agent sessions.
zeta trace log --all-sessions

# List failed or successful tool calls.
zeta trace tools --failed --all-sessions
zeta trace tools --successful --json --all-sessions

# Inspect one agent session.
zeta trace --session agent/issue-triage log
zeta trace --session agent/issue-triage show 4f9d01c2
zeta trace --session agent/issue-triage tree 4f9d01c2 --down

# Compare two prompts component by component.
zeta trace --session agent/issue-triage diff A B --stat

# Rebuild and resend a stored prompt.
zeta trace --session agent/issue-triage replay PROMPT_ID --model fast --diff
```

Every trace id argument accepts a full id, a unique prefix, or a ref such as
`turn/<turn_id>`. `trace replay` verifies the rebuilt prompt payload against the
recorded hash before sending it to the selected model.

A worked walkthrough lives in
[demos/trace-replay.md](demos/trace-replay.md).

## JSON-RPC

`zeta rpc --stdio` serves newline-delimited JSON-RPC 2.0. Each line is one JSON
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

Server notifications:

| Notification | Purpose |
| --- | --- |
| `events.notify` | Carries a persisted runtime event. |
| `tools.call` | Asks the client to execute a registered capability. |

Protocol `0.1` is additive. Clients should ignore unknown result fields and
unknown notification params.

## Commas Shell Frontend

Commas is the shell frontend that ships in this repository. It targets zsh and
wraps Zeta session turns in punctuation shortcuts:

```sh
commas install
commas doctor
```

| Glyph | Workflow | Behavior |
| --- | --- | --- |
| `,` | ask | Answer from local context. |
| `,,` | propose | Run until reviewed shell work is staged or an answer is returned. |
| `,,,` | do | Run the tool loop directly. |
| `+` | run | Execute one explicit command and capture bounded output. |
| `?` | status | Show the current shell session status. |

Examples:

```sh
, "what changed in this repo?"
,, "run the relevant tests"
,,, "update docs and run checks"
+ uv run pytest
?
```

The regular CLI remains available without glyphs:

```text
commas ask [QUESTION]
commas status [--json]
commas log [--touched PATH] [--workflow W] [--since T] [--failed] [--session ID] [--cost] [--json]
commas session [show|path|list|clear|transcript] [--json]
commas model [use|clear]
commas install [--install-dir DIR] [--rc FILE] [--glyphs|--no-glyphs]
commas doctor [--json]
```

Runtime state is inspected with `zeta`: use `zeta events`, `zeta trace`, and
`zeta model list/show`.

Commas and Zeta write shell/frontend and runtime state under `~/.zeta/` by default.
