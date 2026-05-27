# Sigil

Verb-first LLM interaction for the shell, with optional punctuation shortcuts.

![15-second Sigil terminal demo](docs/demo.gif)

Status: Sigil is alpha software. It is ready for early shell users who are
comfortable with local LLM tooling, explicit confirmations, and occasional
interface changes.

The Python package is named `sigil-sh` because `sigil` was not available as a
distribution name. The installed command is still `sigil`.

Sigil has two user-facing surfaces:

- CLI verbs such as `sigil command`, `sigil ask`, `sigil plan`, and
  `sigil patch`.
- Optional shell glyphs such as `,`, `,,`, `?`, and `??`, installed by
  `sigil install`.

The Python package does not expose a public Python API.

## Install

Install the Python command, then install the binding for your shell:

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

You can also install the command with `pipx`:

```sh
pipx install sigil-sh
```

To try the current main branch before a tagged release:

```sh
uv tool install git+https://github.com/rlouf/sigil
```

`sigil install` copies the bundled binding to `~/.sigil/shell/<shell>/` and
adds an idempotent source block to `.zshrc` or `.bashrc`. Running it again
updates the binding without duplicating the rc block.

To install without punctuation shortcuts:

```sh
sigil install zsh --no-glyphs
```

## Requirements

- Python 3.11+
- zsh or Bash for shell bindings
- A local OpenAI-compatible chat completions endpoint for command generation
- `pi` for `sigil ask` and question glyphs
- `glow` for Markdown rendering, optional but recommended
- `fzf` for `sigil command --select`, optional

Useful environment variables:

```sh
SIGIL_MODEL_URL=http://127.0.0.1:8080/v1/chat/completions
SIGIL_MODEL_NAME=local-model
SIGIL_MODEL_PATH=/path/to/model.gguf
SIGIL_STATE_DIR=$HOME/.sigil
SIGIL_GLOW_STYLE=notty
SIGIL_GLOW_WIDTH=88
```

By default, command generation expects a local OpenAI-compatible endpoint at
`http://127.0.0.1:8080/v1/chat/completions`. The question route uses `pi` with
`read,web_search` tools.

## Quick Start

Once the shell binding is installed, use glyphs directly:

```sh
, find files over 10 MB in this repo excluding .git
,, run the relevant tests
? what changed in this repo?
?? what should I run next?
```

Use stdin as context:

```sh
git diff | ? review risky changes
git diff --name-only | , run the relevant tests
```

When stdin is piped into comma or question routes, Sigil previews the input and
asks before using it.

## Shell Glyphs

Installed zsh and Bash bindings expose these optional shortcuts:

```text
,    recommend one command or patch action
,,   generate and run one command, or preview and confirm one patch
,,,  create or resume a durable plan, one confirmed step at a time
?    ask a fresh read/web question
??   follow up on the previous question in the same shell session
???  ask for a more exhaustive read-only answer
```

Examples:

```sh
, find wav files
,, run the relevant tests
,,, clean up this branch and verify it
? why did this git command fail?
?? what should I try first?
??? explain the tradeoffs in detail
```

`,` prints a proposal and, when the proposal is a command, the shell binding
adds that command to shell history for review and editing. `,,` executes command
proposals through your shell. Patch proposals are shown first and applied only
after confirmation. `,,,` stores a plan and asks for confirmation before each
step; each invocation runs at most one accepted step.

## CLI Commands

```text
sigil command [--select] [--json] [PROMPT]
sigil ask [--follow-up] [--json] [QUESTION]
sigil plan [show|resume|abort] [--json]
sigil patch [show|check|apply] [--json] [--yes]
sigil events [--limit N] [--json] [--raw]
sigil events lineage [EVENT_ID] [--json]
sigil session [show|path|list|clear] [--json]
sigil install {zsh|bash} [--install-dir DIR] [--rc FILE] [--glyphs|--no-glyphs]
sigil doctor [--shell auto|zsh|bash] [--json]
```

Examples:

```sh
sigil command "find files over 10 MB in this repo excluding .git"
sigil command --select "show the largest directories"
sigil ask "what changed in this repo?"
sigil ask --follow-up "what should I run next?"
git diff | sigil ask "review risky changes"
git diff --name-only | sigil command "run the relevant tests"
```

See [docs/cli.md](docs/cli.md) for the user-facing CLI contract and JSON
examples.

## Plans and Patches

Create a durable plan with the triple-comma glyph:

```sh
,,, migrate this package to the new API and run the tests
```

Inspect or continue it later:

```sh
sigil plan show
sigil plan resume
sigil plan abort
```

When `,,` produces a unified diff, Sigil stores it as the latest patch preview
for the current shell session:

```sh
sigil patch show
sigil patch check
sigil patch apply --yes
```

`patch check` and `patch apply` run `git apply --check` and `git apply` in the
working directory where the preview was created.

## State

Sigil writes state under `~/.sigil/` by default. Set `SIGIL_STATE_DIR` to move
it.

Current user-visible state:

```text
events.jsonl                              global event log
sessions/<session-id>/last-failure.json   latest failed shell command
sessions/<session-id>/last-patch.json     latest patch preview
sessions/<session-id>/last-plan.jsonl     durable plan snapshots
sessions/<session-id>/last-question.jsonl same-session question transcript
sessions/<session-id>/last-tools.jsonl    latest Pi tool trace
sessions/<session-id>/recent-turns.jsonl  recent shell turns recorded by bindings
```

Installed Bash and zsh bindings set `SIGIL_SESSION_ID` once when the shell
starts, so separate terminal windows keep separate continuity. You can override
the boundary with `SIGIL_SESSION_ID` or `SIGIL_SESSION_DIR`.

Inspect state without calling a model:

```sh
sigil session show
sigil session path
sigil session list
sigil session clear
sigil events
sigil events lineage
```

## Trust Model

Sigil records command suggestions, question answers, patch previews, and plan
steps with trust metadata. The important user model is:

```text
, and ,, are model-authored command/patch routes.
? and ?? are read/web question routes with no execute path.
,,, executes only one confirmed plan step at a time.
```

For details, see [docs/security-lattice.md](docs/security-lattice.md).
