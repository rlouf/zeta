# Sigil

[![CI](https://github.com/rlouf/sigil/actions/workflows/ci.yml/badge.svg)](https://github.com/rlouf/sigil/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sigil-sh.svg)](https://pypi.org/project/sigil-sh/)
[![Python](https://img.shields.io/pypi/pyversions/sigil-sh.svg)](https://pypi.org/project/sigil-sh/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Natural-language shell assistant.

Sigil turns short terminal intents into explicit, inspectable shell actions.
Ask from local context, authorize web search, propose one command, delegate one
agent step, or pursue a bounded goal without leaving your prompt.

![15-second Sigil terminal demo](docs/demo.gif)

```sh
, find files over 10 MB in this repo excluding .git
,, run the relevant tests
? what changed in this repo?
?? what changed upstream in the latest release?
+ cargo test
@ fix the failing parser test
```

Sigil is alpha software. It is ready for early shell users who are comfortable
with local LLM tooling, explicit confirmations, and occasional interface
changes.

## Why Sigil?

Most shell assistants blur together three very different operations:
suggesting, executing, and explaining. Sigil keeps those routes separate.

| Need | Glyph | What happens |
| --- | --- | --- |
| "Give me the command." | `,` | Proposes one command. Nothing runs. |
| "Do one agent turn." | `,,` | Runs one Pi invocation after confirmation. |
| "Do one routine turn." | `,,,` | Runs one Pi invocation without per-step confirmation. |
| "Answer from local context." | `?` | Read-only answer with the read and search tools. No shell is exposed. |
| "Answer with web." | `??` | Read-only answer with the read, search, and web search tools. |
| "Run and capture this command." | `+` | Runs one explicit command, streams output, and records stdout/stderr snippets. |
| "Work toward a goal." | `@` | Runs a bounded goal loop with checkpoints. |
| "Continue routinely." | `@@` | Runs a bounded goal loop with steps auto-approved. |

The result is a shell workflow with small blast radius, durable state, and a
plain CLI underneath the punctuation.

## Install

Install the Python command, then install the shell binding:

```sh
uv tool install sigil-sh
sigil install zsh
sigil doctor
```

For Bash:

```sh
uv tool install sigil-sh
sigil install bash
sigil doctor --shell bash
```

You can also install with `pipx`:

```sh
pipx install sigil-sh
```

To try the current main branch before a tagged release:

```sh
uv tool install git+https://github.com/rlouf/sigil
```

The Python package is named `sigil-sh` because `sigil` was not available as a
distribution name. The installed command is still `sigil`.

`sigil install` copies the bundled binding to `~/.sigil/shell/<shell>/` and
adds an idempotent source block to `.zshrc` or `.bashrc`. Running it again
updates the binding without duplicating the rc block.

## Requirements

- Python 3.11+
- zsh or Bash for shell bindings
- A local OpenAI-compatible chat completions endpoint for command generation
  and Pi-backed routes (default `http://127.0.0.1:8080/v1/chat/completions`)
- `pi`, the [pi-mono](https://github.com/earendil-works/pi) coding-agent CLI,
  for `?`, `??`, `,,`, `,,,`, `@`, and `@@`. Only `,` works without it. Install
  it with:

  ```sh
  curl -fsSL https://pi.dev/install.sh | sh
  # or: npm install -g --ignore-scripts @earendil-works/pi-coding-agent
  ```

  Then point Pi at your model and confirm Sigil can see it with `sigil doctor`.
- `glow` for Markdown rendering, optional but recommended

Useful environment variables:

```sh
SIGIL_MODEL_URL=http://127.0.0.1:8080/v1/chat/completions
SIGIL_MODEL_NAME=local-model
SIGIL_MODEL_PATH=/path/to/model.gguf
SIGIL_STATE_DIR=$HOME/.sigil
SIGIL_RUN_CAPTURE_BYTES=6000
SIGIL_GLOW_STYLE=notty
SIGIL_GLOW_WIDTH=88
```

## Quick Start

Once the shell binding is installed, use the glyphs directly:

```sh
# Propose one command. In zsh, the command is inserted into the prompt buffer.
, find wav files larger than 50 MB

# Run one confirmed agent step.
,, run the relevant tests

# Ask from local read-only context.
? why did the last command fail?

# Ask with web search authorized.
?? what changed in the latest release?

# Run one command through Sigil's explicit capture path.
+ cargo test

# Pursue a goal with checkpoints.
@ fix the failing parser test
```

Use stdin as context:

```sh
git diff | ? review risky changes
git diff --name-only | , run the relevant tests
```

When stdin is piped into comma routes, Sigil previews the input and asks
before using it. Question routes use piped input directly because they have
no execute path.

## A Typical Flow

```sh
# 1. Ask what changed.
? summarize this repo state

# 2. Ask for the smallest useful command.
, run the focused tests for this change

# 3. Let Sigil run exactly one action.
,, run the focused tests

# 4. Audit what happened.
sigil events
```

Sigil stores command suggestions, question answers, and act steps in an
inspectable event log so you can review the route each event came from.

## Glyph Reference

Installed zsh and Bash bindings expose these shortcuts:

| Glyph | Name | Behavior |
| --- | --- | --- |
| `,` | recommend | Recommend one command. |
| `,,` | step | Run one agent turn, confirming effects. |
| `,,,` | auto step | Run one agent turn, auto-approving routine effects. |
| `?` | answer | Answer from local read-only context. |
| `??` | web answer | Answer from local context plus web search. |
| `+` | run | Run one explicit command and capture stdout/stderr snippets. |
| `@` | goal | Run a bounded goal loop with checkpoints. |
| `@@` | auto goal | Run a bounded goal loop with routine auto-approval. |

Examples:

```sh
, find wav files
,, run the relevant tests
,,, fix the failing parser test
? why does git say this branch diverged?
?? what does the remote branch contain?
+ cargo test
@ fix the failing parser test
@@ update docs and run checks
```

`,` prints a command proposal. The zsh binding puts it in the editable prompt
buffer with `print -z` and records it in shell history. Bash records it in
history.

`,,` asks before handing the objective to Pi, gives Pi read/search/edit/write
tools, and returns control to the shell after one bounded Pi invocation. At the
confirmation prompt, `e` opens `$VISUAL` or `$EDITOR` with the available tools,
one per line, so tools can be removed before execution. That invocation may
include zero or more tool calls. `,,,` runs the same one-turn route without
routine confirmation. Shell calls inside those turns go through
Sigil's `sigil_shell` Pi tool: Sigil prints the proposed command, asks whether
to run or edit it, streams stdout/stderr to the terminal, records the turn, and
returns the captured output plus exit status back to Pi so the same turn can
continue. `@` and `@@` repeat bounded Pi turns toward a
durable goal until completion, blockage, budget exhaustion, or interruption.
Agent steps always stream Pi's raw tool calls and prose through `glow` or
`cat`; they do not replace the final answer with a compact summary.

Question routes do not expose Bash. If an answer recommends a command, it is
plain answer text, not a tool call or terminal handoff.

`+` runs the command you provide through `sigil run`, streams stdout/stderr live,
preserves the exit status, and records bounded stdout/stderr snippets for later
failure context. It does not use a shell parser; use `sh -c` for pipelines,
redirection, and shell-only syntax.

To install the CLI without punctuation shortcuts:

```sh
sigil install zsh --no-glyphs
```

## Route Model

Each route has a fixed effect on your system:

| Route | Effect | Rule |
| --- | --- | --- |
| `,` | propose | Model-authored proposal only. |
| `,,` | execute-write | One confirmed Pi agent step. |
| `,,,` | execute-write | One auto-approved Pi agent step. |
| `@` | execute-write | Bounded goal loop with checkpoints. |
| `@@` | execute-write | Bounded goal loop with routine auto-approval. |
| `?` | read-only | Local answer route with no Bash tool. |
| `??` | read-only | Read, search, plus web answer route with no Bash tool. |
| `+` | execute | Explicit local command execution with stdout/stderr capture. |

Every route records what it did to the event log. Inspect it with:

```sh
sigil events
```

## CLI

The glyphs are thin shell functions over a regular CLI:

```text
sigil command [--json] [PROMPT]
sigil run COMMAND [ARGS...]
sigil events [--limit N] [--json] [--raw]
sigil session [show|list|clear] [--json]
sigil status [--json]
sigil install {zsh|bash} [--install-dir DIR] [--rc FILE] [--glyphs|--no-glyphs]
sigil doctor [--shell auto|zsh|bash] [--json]
```

Copy-pasteable examples:

```sh
sigil command "find files over 10 MB in this repo excluding .git"
sigil command "show the largest directories"
git diff --name-only | sigil command "run the relevant tests"
sigil run cargo test
sigil status
sigil events
```

See [docs/cli.md](docs/cli.md) for the user-facing CLI contract and JSON
examples.

## State

Sigil writes event-sourced state under `~/.sigil/` by default. Set
`SIGIL_STATE_DIR` to move it.

Installed Bash and zsh bindings set `SIGIL_SESSION_ID` once when the shell
starts, so separate terminal windows keep separate continuity. Override the
boundary with `SIGIL_SESSION_ID` or `SIGIL_SESSION_DIR`.

Inspect state without calling a model:

```sh
sigil session show
sigil session list
sigil session clear
sigil events
```

## Project Scope

Sigil is:

- A command-line tool and optional shell binding.
- A local-model command proposal route.
- A Pi-backed question and one-step edit route.
- An evented state layer for shell continuity and audit history.

Sigil is not:

- A public Python library. The Python package does not expose a supported API.
- A background autonomous agent.
- A replacement for reviewing commands and model output.

## Roadmap

`sigil sh` is the likely next shell-shaped surface once explicit command
execution proves itself. The shell hooks are intentionally lightweight: they can
record command metadata, but they should not invisibly interpose on every
program's terminal output. A future shell frontend would own the prompt and
transcript boundary, delegate command semantics to the user's real shell, and
decide deliberately when a command runs as structured captured output versus an
interactive terminal session.

## Development

Set up the repo:

```sh
uv sync --group dev
```

Run the checks used by CI:

```sh
uv run pre-commit run --all-files
uv run pytest
```

Render deterministic demo GIFs:

```sh
scripts/render-demo-gifs.sh
```

Demo tapes live in [docs/demos](docs/demos/). They run the real Sigil CLI
from this checkout while shimming only external dependencies such as the
model server, `pi`, and `uv`.

## License

Apache-2.0. See [LICENSE](LICENSE).
