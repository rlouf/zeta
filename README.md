# Sigil

[![CI](https://github.com/rlouf/sigil/actions/workflows/ci.yml/badge.svg)](https://github.com/rlouf/sigil/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sigil-sh.svg)](https://pypi.org/project/sigil-sh/)
[![Python](https://img.shields.io/pypi/pyversions/sigil-sh.svg)](https://pypi.org/project/sigil-sh/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Natural-language shell assistant.

Sigil turns short terminal intents into explicit, inspectable shell interactions.
Ask from local context, hand one agent step to Zeta, or run one command with
captured output without leaving your prompt.
Sigil is inspired by IRC-style bot commands: lightweight punctuation prefixes
that let you address an assistant inline without leaving the conversation.

```sh
, what changed in this repo?
,, run the relevant tests
,,, update the docs and run checks
+ cargo test
```

Sigil is alpha software. It is ready for early shell users who are comfortable
with local LLM tooling, editable command handoffs, and occasional interface
changes.

## Why Sigil?

Most shell assistants blur together three very different operations:
suggesting, executing, and explaining. Sigil keeps those routes separate.

| Need | Glyph | What happens |
| --- | --- | --- |
| "Answer from context." | `,` | Read-only answer with local inspection tools. No shell is exposed. |
| "Do one agent turn." | `,,` | Runs one shell-owned Zeta turn. Bash calls are staged in your prompt. |
| "Do another agent turn." | `,,,` | Same Zeta turn route in v1; reserved for a faster routine path. |
| "Run and capture this command." | `+` | Runs one explicit command, streams output, and records stdout/stderr snippets. |

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
  and Zeta-backed answer/agent routes (default
  `http://127.0.0.1:8080/v1/chat/completions`)
- The `zeta` entrypoint installed with Sigil. `sigil doctor` checks that both
  `sigil` and `zeta` are visible on PATH.
- `glow` for Markdown rendering, optional but recommended

Useful environment variables:

```sh
ZETA_MODEL_URL=http://127.0.0.1:8080/v1/chat/completions
ZETA_MODEL_NAME=local-model
ZETA_MODEL_PATH=/path/to/model.gguf
SIGIL_STATE_DIR=$HOME/.sigil
SIGIL_RUN_CAPTURE_BYTES=6000
SIGIL_GLOW_STYLE=notty
SIGIL_GLOW_WIDTH=88
```

## Quick Start

Once the shell binding is installed, use the glyphs directly:

```sh
# Ask from read-only context.
, why did the last command fail?

# Run one shell-owned Zeta step.
,, run the relevant tests

# Run one command through Sigil's explicit capture path.
+ cargo test

```

Use stdin as context:

```sh
git diff | , review risky changes
git diff --name-only | , what should I test?
```

Read-only comma uses piped input directly because it has no execute path.
Agent-step routes are driven by the prompt text and the current shell session.

## A Typical Flow

```sh
# 1. Ask what changed.
, summarize this repo state

# 2. Ask Zeta to pick the next shell step.
,, run the focused tests for this change

# 3. Edit or run the staged shell command normally.
uv run pytest tests/test_shell_bindings.py

# 4. Resume the Zeta turn with the recorded shell result.
,,
```

Sigil keeps session state under `~/.sigil/` so Zeta can resume from recent
answer turns, handoff transcripts, and command results recorded through `+` or a
Zeta handoff capture window.

## Glyph Reference

Installed zsh and Bash bindings expose these shortcuts:

| Glyph | Name | Behavior |
| --- | --- | --- |
| `,` | read | Answer from read-only context. |
| `,,` | step | Run or resume one shell-owned Zeta turn. |
| `,,,` | auto step | Same v1 Zeta turn route as `,,`. |
| `+` | run | Run one explicit command and capture stdout/stderr snippets. |

Examples:

```sh
, summarize this repo state
,, run the relevant tests
,,, fix the failing parser test
+ cargo test
```

`,` prints a read-only answer. It does not stage commands or write to shell
history.

`,,` gives the objective to the shell-owned Zeta loop. The loop may call local
tools such as `read`, `ls`, `grep`, `edit`, and `write`. Tool calls are shown as
muted trace lines, and tool results are summarized compactly. The full JSON
result stays in the Zeta transcript for the model.

Shell commands are different. Zeta's `bash` tool does not run invisibly. It
stages the proposed command into your editable prompt and returns control to the
shell:

```text
❯ bash   uv run pytest tests/test_shell_bindings.py
  staged in prompt
```

You can run it, edit it, run other shell commands, or reject it. Empty `,,`
resumes the active Zeta step and attaches the recorded shell turns as the source
of truth. If you changed the staged command, Zeta receives that as a changed
handoff rather than assuming the original command ran.

`,,,` currently uses the same v1 Zeta loop as `,,`. It remains a separate glyph
so a more automatic routine path can be split out later.

Read-only routes do not expose Bash. If an answer recommends a command, it is
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
| `,` | read-only | Local answer route with no Bash tool. |
| `,,` | read/write/handoff | One shell-owned Zeta step; Bash is staged in the prompt. |
| `,,,` | read/write/handoff | Same v1 route as `,,`. |
| `+` | execute | Explicit local command execution with stdout/stderr capture. |

Sigil stores audit/debug events and per-shell continuity under `~/.sigil/`.
Inspect the global event log with:

```sh
sigil events
```

## CLI

The glyphs are thin shell functions over a regular CLI:

```text
sigil command [--json] [PROMPT]
sigil ask [--follow-up] [--json] [QUESTION]
sigil run COMMAND [ARGS...]
sigil act [show|resume|abort] [--json]
sigil events [--limit N] [--json] [--raw]
sigil session [show|path|list|clear] [--json]
sigil install {zsh|bash} [--install-dir DIR] [--rc FILE] [--glyphs|--no-glyphs]
sigil doctor [--shell auto|zsh|bash] [--json]
zeta model stream
zeta tools list --json
zeta tool {read|ls|grep|bash|edit|write}
zeta transcript {append|shell-turn|shell-result|tail}
```

Copy-pasteable examples:

```sh
sigil ask "what changed in this repo?"
sigil run cargo test
zeta tools list --json
sigil events
```

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
- A shell-owned Zeta loop for one-step read/search/edit/write workflows.
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

## License

Apache-2.0. See [LICENSE](LICENSE).
