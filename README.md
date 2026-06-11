# Sigil

[![CI](https://github.com/rlouf/sigil/actions/workflows/ci.yml/badge.svg)](https://github.com/rlouf/sigil/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sigil-sh.svg)](https://pypi.org/project/sigil-sh/)
[![Python](https://img.shields.io/pypi/pyversions/sigil-sh.svg)](https://pypi.org/project/sigil-sh/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Natural-language shell assistant.

Sigil turns short terminal intents into explicit, inspectable shell interactions.
Ask from local context, propose one reviewed agent step, do one routine step, or
run one command with captured output without leaving your prompt.
Sigil is inspired by IRC-style bot commands: lightweight punctuation prefixes
that let you address an assistant inline without leaving the conversation.

```sh
, what changed in this repo?
,, run the relevant tests
,,, update the docs and run checks
+ cargo test
?
```

Sigil is alpha software. It is ready for early shell users who are comfortable
with local LLM tooling, editable command handoffs, and occasional interface
changes.

## Why Sigil?

Most shell assistants blur together three very different operations:
suggesting, executing, and explaining. Sigil keeps those workflows separate.

| Verb | Glyph | What happens |
| --- | --- | --- |
| ask | `,` | Answer from local context. No shell is exposed. |
| propose | `,,` | Run the agent until it can stage reviewed shell work or return an answer. |
| do | `,,,` | Run one auto-approved agent step; exact replacements are applied directly. |
| run | `+` | Run one explicit command, stream output, and record stdout/stderr snippets. |
| status | `?` | Show the current session status without calling a model. |

The result is a shell workflow with small blast radius, durable state, and a
plain CLI underneath the punctuation.

## Install

Sigil targets zsh. Install the Python command, then install the shell binding:

```sh
uv tool install sigil-sh
sigil install
sigil doctor
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

`sigil install` copies the bundled binding to `~/.sigil/shell/zsh/` and adds an
idempotent source block to `.zshrc`. Running it again updates the binding
without duplicating the rc block.

## Requirements

- Python 3.11+
- zsh for shell bindings
- A local OpenAI-compatible chat completions endpoint for command generation
  and Zeta-backed ask/agent workflows (default
  `http://127.0.0.1:8080/v1/chat/completions`)

Useful environment variables:

```sh
ZETA_MODEL_URL=http://127.0.0.1:8080/v1/chat/completions
ZETA_MODEL_NAME=local-model
ZETA_MODEL_PATH=/path/to/model.gguf
# Client-side stream idle timeout in seconds (default 120); <=0 disables it.
ZETA_MODEL_IDLE_TIMEOUT_SECONDS=120
# Limit on connect plus time to first chunk (default 600); <=0 disables it.
ZETA_MODEL_FIRST_OUTPUT_TIMEOUT_SECONDS=600
SIGIL_STATE_DIR=$HOME/.sigil
SIGIL_RUN_CAPTURE_BYTES=6000
```

Sigil sends Zeta model requests with OpenAI-compatible streaming enabled
internally, even though it still renders the final assistant message as one
response. For local `llama-server`, this gives the server a direct client
disconnect signal if Sigil aborts a request. The two timeouts are client-side
stream read timeouts: `ZETA_MODEL_FIRST_OUTPUT_TIMEOUT_SECONDS` covers connect
plus prompt processing (a long prefill sends nothing), and
`ZETA_MODEL_IDLE_TIMEOUT_SECONDS` bounds silence between chunks once output
flows; `llama-server --timeout` is a read/write timeout, not a generation
cancellation guarantee.

## Changing Models Mid-Session

Sigil can switch Zeta model profiles for the current terminal session without
changing global environment variables. Define profiles in `~/.zeta/models.toml`:

```toml
[[models]]
name = "fast"
model = "qwen2.5-coder"
url = "http://127.0.0.1:8080/v1/chat/completions"

[[models]]
name = "deep"
model = "qwen3-coder"
url = "http://127.0.0.1:8081/v1/chat/completions"
```

Then select a profile for the active shell session:

```sh
sigil model list
sigil model use fast
, why did the last command fail?

sigil model use deep
,, refactor the failing path and run the focused tests

sigil model show
sigil model clear
```

The selected profile is scoped to the current `SIGIL_SESSION_ID`, so another
terminal keeps its own model selection. Clearing the profile returns the session
to `ZETA_MODEL_NAME` and `ZETA_MODEL_URL`.

`?` always shows the model the next request will use and where the selection
comes from — `(session)` for a profile selected with `sigil model use`,
`(env)` for the `ZETA_MODEL_*` defaults:

```text
clean
model: fast -> qwen2.5-coder @ http://127.0.0.1:8080/v1/chat/completions (session)
```

## Quick Start

Once the shell binding is installed, use the glyphs directly:

```sh
# Ask from local context.
, why did the last command fail?

# Propose one reviewed agent step.
,, run the relevant tests

# Run one command through Sigil's explicit capture path.
+ cargo test

# Check current Sigil status.
?

```

Use stdin as context:

```sh
git diff | , review risky changes
git diff --name-only | , what should I test?
```

Read-only comma uses piped input directly because it has no execute path.
Agent-step workflows are driven by the prompt text and the current shell session.

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
ask turns, handoff timeline events, and command results recorded through `+`.
`sigil session transcript` renders that conversation back as a transcript —
questions, answers, and compact tool traces, with each answer tagged by the
id of the exact prompt the model saw.

The zsh binding also records every interactive command: the command line,
exit status, working directory, and timestamp — never its output. Output is
only captured when you ask for it explicitly with `+`. As with zsh history,
a command typed with a leading space is not recorded, and `SIGIL_RECORD=0`
turns recording off; secrets typed into command arguments are exposed
exactly as they are in `~/.zsh_history`, and the same escape hatches apply.
Recording feeds the session log and the delegation ledger; prompts sent to
the model only ever include a bounded window of recent commands.

## Glyph Reference

Installed zsh bindings expose these shortcuts:

| Glyph | Name | Behavior |
| --- | --- | --- |
| `,` | ask | Answer from local context. |
| `,,` | propose | Run until Sigil can stage reviewed shell work or return an answer. |
| `,,,` | do | Run auto-approved tool calls until no more are needed. |
| `+` | run | Run one explicit command and capture stdout/stderr snippets. |
| `?` | status | Show the current session status and the active model. |

Examples:

```sh
, summarize this repo state
,, run the relevant tests
,,, fix the failing parser test
+ cargo test
?
```

`,` prints a read-only answer. It does not stage commands.

`,,` proposes the next reviewed step. The loop may call local
tools such as `read`, `ls`, `grep`, `bash`, `edit`, and `write` until the model
returns a final answer. Tool calls are shown as muted trace lines, and tool
results are summarized compactly. The full JSON result stays in the Zeta run
timeline for the model.

`,,,` does the same tool loop without the confirmation step. This is YOLO
mode; see the trust note under Workflow Model.

Read-only workflows do not expose Bash. If an answer recommends a command, it is
plain answer text, not a tool call or terminal handoff.

`+` runs the command you provide through `sigil run`, streams stdout/stderr live,
preserves the exit status, and records bounded stdout/stderr snippets for later
failure context. In interactive zsh, the binding captures the raw `+ ...`
prompt line before zsh parses it, so pipelines, redirection, and shell grammar
can be written naturally:

```sh
+ cargo test --all | tee test.log
+ git status --short > status.txt
```

The capture happens in the line editor (a zle widget), and the command runs
inside it: job control does not apply, so Ctrl-Z cannot suspend a `+` command
and it never appears in `jobs`. The widget is also the only `+` path — in
scripts and non-interactive shells, `+` does not dispatch.

From Bash or scripts, `sigil run COMMAND [ARGS...]` keeps argv-style execution.
Use `sigil run --shell 'COMMAND | WITH SHELL GRAMMAR'` when you need shell
parsing from the CLI.

To install the CLI without punctuation shortcuts:

```sh
sigil install --no-glyphs
```

## Workflow Model

Each workflow has a fixed effect on your system:

| Workflow | Effect | Rule |
| --- | --- | --- |
| `,` ask | read-only | Local ask workflow with no Bash tool. |
| `,,` propose | read/write/execute | Read-only tools run directly; Bash/edit/write are staged for review. |
| `,,,` do | read/write/execute | Read-only tools, Bash, edit, and write run directly. |
| `+` run | execute | Explicit local command execution with stdout/stderr capture. |
| `?` status | read-only | Current session status without calling a model. |

`,,,` is YOLO mode: nothing is staged and there is no filesystem boundary.
Tools run with your user's permissions and can read or write anywhere your
user can — the trust model is local user, local trust. When you want to
review every effect before it happens, use `,,`, which stages all writes and
commands at your prompt. For an OS-enforced boundary, launch the CLI inside
a sandbox: [bubblewrap](https://github.com/containers/bubblewrap) on Linux,
or the built-in `sandbox-exec(1)` on macOS.

Sigil stores audit/debug events and per-shell continuity under `~/.sigil/`.
Inspect the global event log with:

```sh
sigil events
```

## CLI

The glyphs are thin shell functions over a regular CLI:

```text
sigil ask [--json] [QUESTION]
sigil run [--shell] COMMAND [ARGS...]
sigil status [--json]
sigil events [--limit N] [--json] [--raw]
sigil session [show|path|list|clear|transcript] [--json]
sigil model [list|use|show|clear]
sigil zeta trace [log|show|tree|closure|refs|prompts]  # ids accept refs and unique prefixes
sigil install [--install-dir DIR] [--rc FILE] [--glyphs|--no-glyphs]
sigil doctor [--json]
```

The bundled Zeta agent runtime is an internal Python package; Sigil workflows run
it in-process. There is no separate `zeta` command.

From shells without the zsh binding, agent steps can be scripted through the
same command the binding uses: `sigil zeta-step --glyph ",," "OBJECTIVE"`
stages reviewed shell work and `sigil zeta-step --continue` resumes a pending
handoff (hidden from `--help` because the binding is the primary surface).

Copy-pasteable examples:

```sh
sigil ask "what changed in this repo?"
sigil run cargo test
sigil events
```

### Exit Codes

- `sigil run` mirrors the exit status of the command it ran: 127 when the
  command is missing, 128+N when it died from signal N (so 130 after
  Ctrl-C).
- `sigil status` (`?`) exits 1 when the session needs attention — the last
  recorded command failed — and 0 when clean.
- `sigil ask` and `sigil zeta-step` (`,`, `,,`, `,,,`) exit 69 when the
  model endpoint is down or fails mid-answer (sysexits `EX_UNAVAILABLE`);
  `sigil doctor` diagnoses the endpoint.
- `sigil model list` exits 1 when the profile config has diagnostics, and
  `sigil doctor` exits 1 when a check fails, even though both still print
  their report.
- Any command exits 127 when an executable it needs is missing and 1 on
  filesystem permission errors.

## State

Sigil writes event-sourced state under `~/.sigil/` by default. Set
`SIGIL_STATE_DIR` to move it.

Every delegation leaves a ledger record in `events.jsonl`: one
`sigil.turn.v1` event per turn — which workflow ran, the objective, the
enforced tool contract, model cost, the outcome, and the ids of the exact
prompts the model saw — plus one `sigil.effect.v1` event per side effect:
files written or edited (with before/after content hashes), commands
executed (with exit status), and staged handoffs with how they resolved.
Plain shell commands and `+` runs are recorded as `run` turns with a
command effect. The log rotates at 10MB, keeping one generation.

The ledger is also indexed into `ledger.sqlite3` next to the event log: a
derived SQLite view (`turns` and `effects` tables) written as records are
appended and rebuildable at any time with `sigil log reindex`, so a
rotated event log loses no turn, effect, or cost answer. Agent turns are
additionally bridged into the session's trace graph as `turn` objects
linking the prompts the model saw and the tool results behind each
effect; the `turn/<turn_id>` ref makes them addressable through `sigil
zeta trace show`. Clearing a session removes its continuity files and
trace store; the ledger index and event log are global and survive
`sigil session clear`.

Installed zsh bindings set `SIGIL_SESSION_ID` once when the shell
starts, so separate terminal windows keep separate continuity. Override the
boundary with `SIGIL_SESSION_ID` or `SIGIL_SESSION_DIR`.

Inspect state without calling a model:

```sh
sigil session show
sigil session list
sigil session clear
sigil events
sigil log reindex
```

The trace store underneath is explorable the same way. `sigil zeta trace
log` lists recent prompts and assistant messages, one line per object
(`--kind`/`--all` widen it to tool calls, results, and run events);
`trace show ID` renders one object with its body and its derivations in
both directions; `trace tree ID` walks what produced an object
(`--down` for what came of it). Every ID argument accepts a ref name, a
full id, or a unique prefix — three commands take you from "what
happened" to the exact bytes the model saw:

```sh
sigil zeta trace log
sigil zeta trace show 4f9d01c2
sigil zeta trace tree 4f9d01c2 --down
```

## Project Scope

Sigil is:

- A command-line tool and optional shell binding.
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
timeline boundary, delegate command semantics to the user's real shell, and
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
