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
, "what changed in this repo?"
,, "run the relevant tests"
,,, "update the docs and run checks"
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
ZETA_MODEL_PATH=/path/to/model.gguf
# Client-side stream idle timeout in seconds (default 120); <=0 disables it.
ZETA_MODEL_IDLE_TIMEOUT_SECONDS=120
# Limit on connect plus time to first chunk (default 600); <=0 disables it.
ZETA_MODEL_FIRST_OUTPUT_TIMEOUT_SECONDS=600
SIGIL_STATE_DIR=$HOME/.sigil
SIGIL_RUN_CAPTURE_BYTES=6000
```

Model endpoints are configured through profiles in `~/.zeta/models.toml`
(below), not environment variables. Without any configuration sigil talks
to `local-model` at the default local endpoint.

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
thinking = "none"
default = true

[[models]]
name = "deep"
model = "qwen3-coder"
url = "http://127.0.0.1:8081/v1/chat/completions"
thinking = "high"
```

At most one profile may set `default = true`: it is the model every new
session starts on. Omit the flag everywhere and sessions start on the
builtin local default (`local-model` @
`http://127.0.0.1:8080/v1/chat/completions`).

`thinking` controls model reasoning per profile, using the reasoning-effort
values of OpenAI's Responses API: `"none"` disables thinking, and
`"minimal"`, `"low"`, `"medium"`, or `"high"` request that effort
(sent as `reasoning_effort`). Omit it to leave the model's own default in
place — thinking models think. While the model thinks, the last few lines
of its reasoning stream as a muted tail under the thinking timer; the tail
is erased the moment the answer or a tool call arrives, leaving a single
muted `thought for 12s` line in scrollback. Set `SIGIL_THINKING_TRACE=0`
to keep only the timer. Reasoning is recorded in the trace and shown in
full by `sigil session transcript`; it is never resent to the model in
later turns, and never written to redirected or piped output.

### Codex profiles

A profile with `api = "codex-responses"` talks to the OpenAI Codex backend
on a ChatGPT subscription instead of a local chat-completions server:

```toml
[[models]]
name = "codex"
model = "gpt-5.5"
api = "codex-responses"
thinking = "high"
```

Authentication reuses the Codex CLI credentials at `~/.codex/auth.json`:
run `codex login` once, and sigil refreshes the access token in place when
it expires. `sigil doctor` reports credential state whenever a codex
profile is configured. Requests carry `originator: zeta`.

This is an explicit departure from the local-first default: while a codex
profile is active, prompts — including file contents read by tools — leave
the machine for OpenAI's backend. Reasoning summaries stream like local
thinking traces; the full chain of thought stays encrypted with OpenAI.

Then select a profile for the active shell session:

```sh
sigil model list
sigil model use fast
, "why did the last command fail?"

sigil model use deep
,, "refactor the failing path and run the focused tests"

sigil model show
sigil model clear
```

The selected profile is scoped to the current `SIGIL_SESSION_ID`, so another
terminal keeps its own model selection. Clearing the profile returns the
session to the `default = true` profile, or to the builtin local default
when no profile claims the flag.

`?` always shows the model the next request will use and where the selection
comes from — `(session)` for a profile selected with `sigil model use`,
`(config)` for the `default = true` profile, `(builtin)` for the
no-configuration fallback:

```text
clean
model: fast -> qwen2.5-coder @ http://127.0.0.1:8080/v1/chat/completions (session)
```

If the selected profile has since been removed from `models.toml`, the line
says so — `(builtin; profile 'fast' missing from models.toml)` — instead of
pretending no selection was made.

## Quick Start

Once the shell binding is installed, use the glyphs directly:

```sh
# Ask from local context.
, "why did the last command fail?"

# Propose one reviewed agent step.
,, "run the relevant tests"

# Run one command through Sigil's explicit capture path.
+ cargo test

# Check current Sigil status.
?

```

Use stdin as context:

```sh
git diff | , "review risky changes"
git diff --name-only | , "what should I test?"
```

Read-only comma uses piped input directly because it has no execute path.
Agent-step workflows are driven by the prompt text and the current shell session.

## A Typical Flow

```sh
# 1. Ask what changed.
, "summarize this repo state"

# 2. Ask Zeta to pick the next shell step. The staged command lands in
#    your prompt as an editable `+ …` line.
,, "run the focused tests for this change"

# 3. Press Enter on the staged line (edit it first if you like).
#    Executing the staged command resumes the Zeta turn with the
#    recorded result — no follow-up keystroke needed.
+ uv run pytest tests/test_shell_bindings.py

# 4. Any other command stays plain capture: explore as long as you
#    want, then resume explicitly. Zeta sees everything you ran.
,,
```

Only the staged command (whitespace changes and appended arguments
included) triggers the automatic resume, and a Ctrl-C'd run never does.
Set `SIGIL_AUTO_CONTINUE=0` to always resume by hand with a bare `,,`.

Sigil keeps session state under `~/.sigil/` so Zeta can resume from recent
ask turns, handoff timeline events, and command results recorded through `+`.
`sigil session transcript` renders that conversation back as a transcript —
questions, answers, and compact tool traces, with each answer tagged by the
id of the exact prompt the model saw. When the model streams reasoning, the
transcript shows it in full as italic text above the answer it led to; the
live loop shows only the ephemeral tail and the `thought for 12s` line.

The zsh binding also records every interactive command: the command line,
exit status, working directory, and timestamp — never its output. Output is
only captured when you ask for it explicitly with `+`. Recording costs
nothing at the prompt: the binding appends to a per-session spool without
starting any process, and the CLI folds the spool in the next time any
`sigil` command runs. As with zsh history, a command typed with a leading
space is not recorded, and `SIGIL_RECORD=0` turns recording off; secrets
typed into command arguments are exposed exactly as they are in
`~/.zsh_history`, and the same escape hatches apply. Recording feeds the
session log and the delegation ledger; prompts sent to the model only ever
include a bounded window of recent commands.

## Glyph Reference

Installed zsh bindings expose these shortcuts:

| Glyph | Name | Behavior |
| --- | --- | --- |
| `,` | ask | Answer from local context. |
| `,,` | propose | Run until Sigil can stage reviewed shell work or return an answer. |
| `,,,` | do | Run auto-approved tool calls until no more are needed. |
| `+` | run | Run one explicit command and capture stdout/stderr snippets. |
| `?` | status | Session status: last failure, last delegation, staged work, today's cost, active model. |

Glyph lines are ordinary shell commands; quote your prompts and compose
freely.

Examples:

```sh
, "summarize this repo state"
,, "run the relevant tests"
,,, "fix the failing parser test"
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

`+` runs the command you provide, streams stdout/stderr live,
preserves the exit status, and records bounded stdout/stderr snippets for later
failure context. In interactive zsh, the binding captures the raw `+ ...`
prompt line before zsh parses it, so pipelines, redirection, and shell grammar
can be written naturally:

```sh
+ cargo test --all | tee test.log
+ git status --short > status.txt
```

`,`, `,,`, `,,,`, and `?` are ordinary commands: zsh parses the line, so
quoting, expansion, redirects, and pipes are exactly the shell you already
know. Quote your prompts — it reads better and sidesteps surprises with
apostrophes and globs — and reach for the shell deliberately when you want
it. Double quotes interpolate, which is what staying in the shell buys you:

```sh
, "explain this error: $(tail -1 err.log)"
,, "free space on $HOST, largest caches first"
, "summarize the failing tests" > summary.txt
, "one-line answer: which port does the dev server use" | pbcopy
```

Single quotes keep a prompt fully literal: `, 'what does $PATH contain?'`
asks about the name, not your path. One zsh quirk worth knowing: `!`
immediately before a closing double quote (`, "fix it!"`) trips history
expansion — use single quotes for prompts with exclamation marks.

A `+` command runs as an ordinary foreground job: Ctrl-Z suspends it, it
shows up in `jobs`, `fg` resumes it, and `$?` carries its exit status. The
accepted `+` line keeps showing exactly what you typed, with a dim marker
showing where the handed-off dispatch ran. In scripts and non-interactive
shells the named glyph functions dispatch; `+` is interactive-only.

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
sigil ask [QUESTION]
sigil status [--json]
sigil log [--touched PATH] [--workflow W] [--since T] [--failed] [--session ID] [--cost] [--json]
sigil log [show|reindex|export|import]
sigil blame FILE
sigil events [--limit N] [--json] [--raw]
sigil session [show|path|list|clear|transcript] [--json]
sigil model [list|use|show|clear]
sigil trace [--session ID] [log|grep|show|tree|closure|refs|prompts|diff|replay]
sigil install [--install-dir DIR] [--rc FILE] [--glyphs|--no-glyphs]
sigil doctor [--json]
```

Every command documents itself: `sigil COMMAND --help` states what it reads
and writes and ends with copy-pasteable examples.

The bundled Zeta agent runtime is an internal Python package; Sigil workflows run
it in-process. There is no separate `zeta` command.

From shells without the zsh binding, agent steps can be scripted through the
same command the binding uses: `sigil step --workflow propose "OBJECTIVE"`
stages reviewed shell work and `sigil step --workflow propose --continue` resumes a pending
handoff (hidden from `--help` because the binding is the primary surface).

### Exit Codes

- `+` mirrors the exit status of the command it ran: 127 when the command is
  missing, 128+N when it died from signal N (so 130 after Ctrl-C).
- `sigil status` (`?`) exits 1 when the session needs attention — the last
  recorded command failed — and 0 when clean.
- `sigil ask` and `sigil step` (`,`, `,,`, `,,,`) exit 69 when the
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
trace show`. Clearing a session removes its continuity files and
trace store; the ledger index and event log are global and survive
`sigil session clear`.

Installed zsh bindings set `SIGIL_SESSION_ID` once when the shell starts
and tie it to the terminal's pty, so separate terminal windows — including
tmux panes, which inherit the server's environment — keep separate
continuity, while subshells on the same terminal share it. Override the
boundary with `SIGIL_SESSION_ID` or `SIGIL_SESSION_DIR`; an id you set
yourself is respected as given. `sigil doctor` flags a session id that was
created on a different terminal than the one it is used from.

Inspect state without calling a model:

```sh
sigil session show
sigil session list
sigil session clear
sigil events
sigil log reindex
```

The ledger is the query surface over that record. `sigil log` lists
your turns across every session newest first, each line carrying its
session id (`--session ID` narrows to one shell and drops the column;
`--touched PATH`, `--workflow`, `--since 2d`, `--failed`, and `--cost`
narrow or enrich);
`sigil log show TURN` renders one turn in full — objective, contract,
model, cost, effects with content hashes, and the prompt ids that feed
`sigil trace show`. `sigil blame FILE` lists every turn that wrote
or edited a file through the write/edit tools, with its objective and
prompt ids; bash commands record what ran rather than which files they
touched, so they appear in `sigil log`, not in blame. `?` reads the same
ledger: it shows the last delegation outcome, a pending staged command,
and today's session cost next to the active model.

```sh
sigil log --touched src/app.py --since 2d
sigil blame src/app.py
sigil log show 4f9d01c2
```

`sigil events` stays the raw event view underneath all of this.

The ledger is also the unit of exchange. `sigil log export` writes a
self-contained JSON bundle — the matching turn and effect records plus
each turn's full trace closure (prompts, components, tool results,
with their derivations and `turn/<id>` refs) — and `sigil log import`
restores it on another machine: records join the global event log (so
they survive `log reindex`), objects land in per-session trace stores,
and every query above answers there too. Re-importing is a no-op.

```sh
sigil log export --since 2026-06-01 -o week.json
sigil log import week.json
```

The ask workflow can read the ledger too: `,` carries a read-only
`query_log` tool, so `, what did I delegate yesterday?` answers from
your real delegation history and cites turn ids you can check with
`sigil log show`. The tool searches every session by default and never
writes anything.

The trace store underneath is explorable the same way. `sigil trace
log` lists recent prompts and assistant messages, one line per object
(`--kind`/`--all` widen it to tool calls, results, and run events);
`trace show ID` renders one object with its body and its derivations in
both directions; `trace tree ID` walks what produced an object
(`--down` for what came of it). Every ID argument accepts a ref name, a
full id, or a unique prefix — three commands take you from "what
happened" to the exact bytes the model saw:

```sh
sigil trace log
sigil trace show 4f9d01c2
sigil trace tree 4f9d01c2 --down
```

Because prompts are content-addressed component graphs, two more
questions are one command each. `trace diff A B` compares two prompts
component by component — identical ids are unchanged, changed
components get a text diff (`--stat` for the one-line view). `trace
replay ID` rebuilds the exact request from the stored components,
verifies it against the recorded payload hash, and resends it through
the model boundary — against the session's active model or `--model
PROFILE` — recording the new answer in the trace so replays are
themselves inspectable (`--diff` to diff old and new answers):

```sh
sigil trace diff 4f9d01c2 81be33aa --stat
sigil trace replay 4f9d01c2 --model fast --diff
```

A worked walkthrough with real output lives in
[docs/demos/trace-replay.md](docs/demos/trace-replay.md).

None of this is locked to the current shell. `sigil trace --session ID
…` reads another session's store (read-only — nothing you inspect can
mutate it), `trace log --all-sessions` lists every recorded session's
objects with the session id as a line prefix, and `trace grep PATTERN`
searches object data — so "which session was I in when I asked about
X last week" is one command:

```sh
sigil trace --session ttys004-8812 show 4f9d01c2
sigil trace log --all-sessions
sigil trace grep "rollback" --all-sessions
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
