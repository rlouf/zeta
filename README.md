# Sigil

[![CI](https://github.com/rlouf/sigil/actions/workflows/ci.yml/badge.svg)](https://github.com/rlouf/sigil/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sigil-sh.svg)](https://pypi.org/project/sigil-sh/)
[![Python](https://img.shields.io/pypi/pyversions/sigil-sh.svg)](https://pypi.org/project/sigil-sh/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Natural-language shell assistant.

Sigil turns short terminal intents into explicit, inspectable shell actions.
Ask for one command, run one confirmed command, or ask a read-only question
without leaving your prompt.

![15-second Sigil terminal demo](docs/demo.gif)

```sh
, find files over 10 MB in this repo excluding .git
,, run the relevant tests
? what changed in this repo?
?? what should I run next?
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
| "Do the next step." | `,,` | Runs one generated command. |
| "Make one edit pass." | `,,,` | Runs one confirmed Pi read/edit/write action, then returns control. |
| "Answer this." | `?` | Read-only question with read and web tools. No shell is exposed. |
| "Continue that answer." | `??` | Follows up in the same terminal session. |

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
- `pi` for `?`, `??`, `???`, and `,,,`
- `glow` for Markdown rendering, optional but recommended
- `fzf` for `,` with `--select`, optional

Useful environment variables:

```sh
SIGIL_MODEL_URL=http://127.0.0.1:8080/v1/chat/completions
SIGIL_MODEL_NAME=local-model
SIGIL_MODEL_PATH=/path/to/model.gguf
SIGIL_STATE_DIR=$HOME/.sigil
SIGIL_ENABLE_PROMPT_MARKER=0
SIGIL_GLOW_STYLE=notty
SIGIL_GLOW_WIDTH=88
```

## Quick Start

Once the shell binding is installed, use the glyphs directly:

```sh
# Propose one command. In zsh, the command is inserted into the prompt buffer.
, find wav files larger than 50 MB

# Execute one generated command.
,, run the relevant tests

# Ask a read/web question with no execute path.
? why did the last command fail?

# Follow up in the same shell session.
?? what should I try first?
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
sigil events lineage
```

Sigil stores command suggestions, question answers, and act steps with trust
metadata so you can inspect where a recommendation came from and what it was
allowed to do.

## Glyph Reference

Installed zsh and Bash bindings expose these shortcuts:

| Glyph | Name | Behavior |
| --- | --- | --- |
| `,` | recommend | Recommend one command. |
| `,,` | execute | Generate and run one command. |
| `,,,` | act | Run one confirmed Pi edit action. |
| `?` | ask | Ask a fresh read/web question. |
| `??` | follow up | Continue the previous question in the same shell session. |
| `???` | exhaustive | Ask for a more exhaustive read-only answer. |

Examples:

```sh
, find wav files
,, run the relevant tests
,,, fix the failing parser test
? why does git say this branch diverged?
?? what is the safest next command?
??? explain the release options and their risks
```

`,` prints a command proposal. The zsh binding puts it in the editable prompt
buffer with `print -z` and records it in shell history. Bash records it in
history. `,,` executes command proposals through your shell.

`,,,` asks before handing the objective to Pi, gives Pi read/search/edit/write
tools, and returns control to the shell after one bounded edit pass. Bash
calls inside that pass are blocked and handed off. By default, Sigil shows a
compact tool trace and a short completion summary; use `,,, --verbose ...`
for Pi's raw tool stream and prose.

Question routes do not expose Bash. If an answer recommends a command, it is
plain answer text, not a tool call or terminal handoff.

To install the CLI without punctuation shortcuts:

```sh
sigil install zsh --no-glyphs
```

## Trust Model

Sigil's important user rules are:

| Route | Capability | Rule |
| --- | --- | --- |
| `,` | propose | Model-authored proposal only. |
| `,,` | exec boxed | One generated command. |
| `,,,` | exec/write boxed | One confirmed Pi edit action at a time. |
| `?`, `??`, `???` | read | Read/web question routes with no Bash tool. |

Trust records include route, integrity, capability, taint, provisional
status, and input event ids. Inspect them with:

```sh
sigil events
sigil events lineage
```

For details, see [docs/security-lattice.md](docs/security-lattice.md).

## CLI

The glyphs are thin shell functions over a regular CLI:

```text
sigil command [--select] [--json] [PROMPT]
sigil events [--limit N] [--json] [--raw]
sigil events lineage [EVENT_ID] [--json]
sigil session [show|list|clear] [--json]
sigil status [--json]
sigil install {zsh|bash} [--install-dir DIR] [--rc FILE] [--glyphs|--no-glyphs]
sigil doctor [--shell auto|zsh|bash] [--json]
```

Copy-pasteable examples:

```sh
sigil command "find files over 10 MB in this repo excluding .git"
sigil command --select "show the largest directories"
git diff --name-only | sigil command "run the relevant tests"
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
sigil events lineage
```

## Project Scope

Sigil is:

- A command-line tool and optional shell binding.
- A local-model command proposal route.
- A Pi-backed question and one-step edit route.
- An evented state layer for shell continuity and provenance.

Sigil is not:

- A public Python library. The Python package does not expose a supported API.
- A background autonomous agent.
- A replacement for reviewing commands and model output.

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
