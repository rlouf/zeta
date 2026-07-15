# Zeta

[![CI](https://github.com/rlouf/zeta/actions/workflows/ci.yml/badge.svg)](https://github.com/rlouf/zeta/actions/workflows/ci.yml)
[![Zeta PyPI](https://img.shields.io/pypi/v/zeta-os.svg)](https://pypi.org/project/zeta-os/)
[![Python](https://img.shields.io/pypi/pyversions/zeta-os.svg)](https://pypi.org/project/zeta-os/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Zeta is a local-first substrate for durable agents — the one where you can
replay the exact prompt behind any action. **An agent is a Markdown file.** Every
event it receives, every prompt the model saw, and every tool call it made is
stored in project-local SQLite, so when an agent does something you did not
expect, you replay what actually happened instead of guessing.

## An agent is a Markdown file

A single `agents/<slug>.md` file declares everything an agent is:

- the durable **events** it accepts and may return,
- the **tools** it can use and the shared **skills** it opts into,
- and the **prompt** that runs when a matching event arrives.

The runtime does the rest. It stores events, queue state, run attempts, tool
calls, model calls, and prompt traces locally, so nothing about a run is
hidden — you can list it, diff it, and replay the exact prompt the model saw.

## Quick start

First, point Zeta at a model — it drives every agent. Any OpenAI-compatible
chat completions endpoint works; Zeta looks for one at
`http://127.0.0.1:8080/v1/chat/completions` by default. See
[docs/concepts.md](docs/concepts.md#model-profiles) for model profiles and the
Codex backend.

Install Zeta (Python 3.11+) and make a folder for it to watch:

```sh
uv tool install zeta-os
mkdir -p ~/zeta-inbox
```

Enable the bundled filesystem connector, so a new file becomes a durable event.
`agents/connectors.yaml`:

```yaml
event_connectors:
  - filesystem
```

Now write the agent. It is one Markdown file, `agents/note-reader.md`:

```markdown
---
name: Note Reader
description: Summarizes files dropped into the inbox.
resumable: true
accepts:
  - event: file.created
    filter:
      dir: ~/zeta-inbox
    idempotency_key: "file:{path}"
tools:
  - read
---
A file was just created: {{ event.payload.path }}.
Read it and reply with a one-sentence summary of what it contains.
```

Start the worker and drop a file into the folder:

```sh
zeta serve &                                                   # polls the inbox
echo "Buy milk. Email the accountant about Q3." > ~/zeta-inbox/todo.txt
```

Within a couple of seconds the connector emits `file.created`, `note-reader`
runs, and its whole timeline — every prompt and tool call — is on disk under the
session `agent/note-reader`:

```sh
zeta trace --session agent/note-reader log
```

```text
a1b2c3d4  assistant_message   The note is a short to-do list: buy milk and email the accountant about Q3.
7f8e9d0c  prompt              6 components · ~712 tok
e5f6a7b8  assistant_message   → read
9c0d1e2f  prompt              4 components · ~486 tok
```

Stop the worker with `kill %1` when you are done. Those `prompt` lines are the
interesting part — the next section is what you do with them.

> `zeta agent new <slug>` scaffolds a starting skeleton if you would rather not
> write the file by hand. `zeta run` drives agents once and exits; `zeta serve`
> runs continuously and is what polls connectors like the filesystem watcher.

## Replay any decision

Agents fail in ways you cannot reproduce, because the input that caused the
failure is gone by the time you see the output. In Zeta it is not gone: every
prompt is a stored, hash-verified object you can inspect, resend, and diff
without re-running anything upstream — no file, no connector, no queue.

Say `note-reader` returned a lazy summary. Pull up the exact prompt behind it:

```sh
zeta trace --session agent/note-reader show 9c0d1e2f
```

Resend that same prompt — byte for byte — to a stronger model and diff the two
answers, without touching the inbox or replaying the pipeline:

```sh
zeta trace --session agent/note-reader replay 9c0d1e2f --model deep --diff
```

Changed a skill or the prompt and want to know exactly what moved? Diff the two
prompt versions, component by component:

```sh
zeta trace --session agent/note-reader diff 9c0d1e2f 3a4b5c6d --stat
```

`replay` verifies the rebuilt prompt against the recorded hash before sending, so
you are always comparing against what actually ran — not an approximation of it.
This is what Zeta is built to do: turn *"why did it do that?"* from a guess into
a command.

## Composing agents

Events are the seam between agents: one agent's `returns` is another agent's
`accepts`. That is how you build a pipeline instead of one monolithic prompt.

Here a scheduled agent produces a weekly digest and hands it to a second agent
that posts it. `agents/release-digest.md` runs on a cron and returns an event:

```markdown
---
name: Release Digest
description: Summarizes the pull requests merged this week.
schedules:
  - cron: "0 9 * * 1"
    timezone: Europe/Paris
returns:
  - release.summary.ready
tools:
  - bash
---
Summarize the pull requests merged in the last week as release notes.
```

`agents/announcer.md` waits for that event and acts on it:

```markdown
---
name: Announcer
description: Posts release notes for the team.
accepts:
  - release.summary.ready
tools:
  - write
---
Post these release notes:

{{ event.payload.summary }}
```

The shared event needs a schema, `agents/events/release.summary.ready.json`:

```json
{
  "type": "object",
  "required": ["summary"],
  "properties": { "summary": { "type": "string" } },
  "additionalProperties": false
}
```

A `schedules:` block turns cron into a trigger event,
`agent.release-digest.scheduled`. Because `zeta run` fires due schedules and then
drains the queue, one command drives the whole chain:

```sh
zeta run
# fires the schedule -> release-digest runs -> publishes release.summary.ready
#                    -> announcer runs on that event -> queue empty, exit
```

The hand-off is an ordinary durable event — inspect it with
`zeta events --type-prefix release.`.

## How it works

**Events** are durable records — a `type`, a `source`, an object `payload`, and
optional idempotency and causality metadata. They are the only way work enters
the system. Project event schemas live under `agents/events/`.

**Agents** are the Markdown files in `agents/`. When an event matches an agent's
`accepts`, the runtime runs the assistant/tool loop against the rendered prompt.
That event may come from a connector, a `schedules:` cron trigger, or another
agent's `returns` — which is how agents compose. If the agent declares
`returns`, Zeta performs one final structured generation and publishes the
validated result as a new durable event.

**Tools and skills** extend an agent. Tools (`read`, `grep`, `bash`, `edit`,
`write`, …) are capabilities granted to the model; skills are shared Markdown
procedures under `agents/skills/` that agents opt into.

**Connectors** bind agents to the outside world. They contribute event schemas
and handle ingress (external events in) and egress (returned events out) for
services such as Slack or the local filesystem.

**Durability and replay** is the point of the whole thing. Runtime events answer
*"what happened?"*; prompt traces answer *"what exactly did the model see?"* — and
any stored prompt can be resent (see [Replay any decision](#replay-any-decision)).

Each of these has a full reference in **[docs/concepts.md](docs/concepts.md)**:
the frontmatter fields, event and returned-event mechanics, the tool table,
connector ingress/egress, running the worker and scheduler, observability, and
the JSON-RPC interface.

## Development

```sh
uv sync --group dev
uv run pre-commit run --all-files
uv run pytest
```

## License

Apache-2.0. See [LICENSE](LICENSE).
