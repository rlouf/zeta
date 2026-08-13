# Zeta

[![CI](https://github.com/rlouf/zeta/actions/workflows/ci.yml/badge.svg)](https://github.com/rlouf/zeta/actions/workflows/ci.yml)
[![Zeta PyPI](https://img.shields.io/pypi/v/zeta-os.svg)](https://pypi.org/project/zeta-os/)
[![Python](https://img.shields.io/pypi/pyversions/zeta-os.svg)](https://pypi.org/project/zeta-os/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

## Ambient Agents Runtime

Zeta runs agents that stay with your work.

Define each agent in Markdown. Zeta wakes it when relevant events occur.
Agents can use tools, pass work to other agents, and retain a durable record
of each action.

Use Zeta to give an agent an ongoing responsibility:

- Read and sort each new file in an inbox.
- Prepare a weekly report on a schedule.
- Respond to a Slack message.
- Hand a completed task to another agent.

Your first agent can stay on top of an inbox. You give it the responsibility
once. When a file arrives, it writes a summary where you can use it.

## Native runtime CLI

The Rust native CLI now has an explicit project lifecycle. It validates,
activates, starts, inspects, and stops one project generation. See the
[Native CLI guide](docs/native-cli.md).

## Give an agent its first responsibility

Zeta needs Python 3.11+ and Codex. Run `codex login` once before you start.

Install Zeta:

```sh
uv tool install zeta-os
```

Create the default inbox-summarizer project:

```sh
zeta new ~/zeta-demo
cd ~/zeta-demo
```

`zeta new` creates the agent, its inbox, its summary folder, and its filesystem
connector. It also excludes runtime state from Git.

To use a different model, see [Model Profiles](docs/concepts.md#model-profiles).

Start Zeta in one terminal:

```sh
zeta serve
```

Create a file in a second terminal:

```sh
echo "Buy milk. Send the release notes before Friday." \
  > ~/zeta-demo/inbox/todo.txt
```

Create the file after Zeta starts. The filesystem connector ignores files that
exist before its first poll.

Open the result:

```sh
cat ~/zeta-demo/summaries/todo.txt.md
```

For this file, the summary can be:

```text
The file lists two tasks: buy milk and send release notes before Friday.
```

Zeta detects the file, starts the agent, and writes the summary. You did not
need to ask it again.

Want to inspect the work behind the result?

```sh
zeta traces log --session agent/inbox-summarizer
```

## What an agent is

An agent is one Markdown file in `agents/`.

Its frontmatter declares the events it accepts, the tools it may use, and the
events it may publish. Its body gives the agent instructions.

```md
---
name: Release Digest
description: Prepares the weekly release digest.
schedules:
  - cron: "0 9 * * 1"
    timezone: Europe/Paris
    catchup: latest
publishes:
  - release.summary.ready
returns:
  - release.digest.completed
tools:
  - bash
---
Summarize pull requests merged during the last week.

Write release notes for the team. Use `publish_event` to publish them as
`release.summary.ready`.
```

A schedule creates an event for the agent. `publishes` authorizes intermediate
effects: this agent can call `publish_event` while it works to hand the release
notes to an announcer. `returns` is different: after the normal agent/tool
loop ends, Zeta makes one final no-tools structured generation that selects
`release.digest.completed` and validates its payload against that event's
schema. Zeta then persists and routes that returned event. An agent may declare
both fields.

## Build an agent system

Events connect agents.

One agent can prepare release notes. Another agent can publish them. Each
handoff has a named event and a validated payload.

```text
schedule
  -> release-digest
  -> release.summary.ready
  -> announcer
  -> Slack
```

Put project event schemas in `agents/events/`.

```text
agents/
  release-digest.md
  announcer.md
  events/
    release.summary.ready.json
  skills/
    release-notes.md
```

Use `zeta run` to process pending work and exit.

```sh
zeta run
```

Use `zeta serve` for continuous work, schedules, and connector ingress.

```sh
zeta serve
```

## Give agents the right tools

Zeta includes file and shell tools:

- `read`
- `grep`
- `edit`
- `write`
- `bash`

Agents only receive tools that their Markdown declaration lists.

Shared skills live in `agents/skills/`. A skill is a Markdown procedure that
multiple agents can use.

```md
---
name: Support Triage
description: Sorts support requests.
skills:
  - support-policy
tools:
  - read
  - write
---
Apply the support policy to this request:

{{ event.payload.message }}
```

## Inspect work when it matters

Zeta stores events, queue state, model requests, tool calls, and agent results.
Runtime state lives in the project `.zeta/` directory.

Use the CLI to inspect work:

```sh
zeta ps
zeta sessions list
zeta events list
zeta queue list
zeta attempts list
zeta traces log --all-sessions
```

Inspect the exact prompt for an agent response:

```sh
zeta traces show PROMPT_ID --session agent/inbox-summarizer
```

Replay that prompt with another model profile:

```sh
zeta traces replay PROMPT_ID \
  --session agent/inbox-summarizer \
  --model codex \
  --diff
```

Replay is not the reason to use Zeta. It is how you can trust an ambient agent
after it acts.

## Work with sessions interactively

Run `zeta` without a subcommand to open the terminal interface. It lists the
project's sessions on startup. Press `n` to start a session, `Enter` to attach,
and `Esc` to detach without stopping the agent. Attached sessions continue to
update as the agent works, and you can send another message with `Enter`.

The interface keeps one pane and preserves each session's draft, history,
selection, and scroll position while you switch. Use `/` to find another
session, `1` through `9` to attach directly from the session list, and `?` for
the complete contextual key map.

In the composer, `Enter` sends and `Shift-Enter` inserts a newline. Terminals
that cannot distinguish modified Enter use `Ctrl-J` for a newline instead.
Standard terminal selection remains available; focus transcript items with
`Tab` and press `y` to copy through OSC 52 when the terminal supports it.
Colors, clickable local paths, and copying degrade to plain, readable text when
unsupported, and `NO_COLOR` is respected.

If the local IPC process exits, the interface reconnects automatically. Drafts
and reading positions stay in place, durable events are reconciled first, and
only unresolved messages are retried with their original idempotency keys.
`Ctrl-C` exits, while `q` exits from browse mode. `Ctrl-Z` suspends cleanly on
Unix and redraws after resume.

## Learn more

[Concepts](docs/concepts.md) covers agent files, events, schemas, connectors,
model profiles, schedules, runtime state, and the CLI.

[Trace replay demo](docs/demos/trace-replay.md) shows prompt inspection and
model comparison.

## Development

```sh
uv sync --group dev
uv run pre-commit run --all-files
uv run pytest
```

## License

Apache-2.0. See [LICENSE](LICENSE).
