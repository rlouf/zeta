# Sigil CLI

This document describes Sigil's user-facing command-line API. Human-readable
output may evolve. JSON output is available through explicit `--json` flags and
is the preferred integration surface.

Status, progress, warnings, and errors are written to stderr. JSON output is
written to stdout.

## Top-Level Commands

```text
sigil command [--json] [PROMPT]
sigil ask [--follow-up] [--json] [QUESTION]
sigil act [show|resume|abort] [--json] [--verbose]
sigil events [--limit N] [--json] [--raw]
sigil events list [--limit N] [--json] [--raw]
sigil session [show|path|list|clear] [--json]
sigil status [--json]
sigil install {zsh|bash} [--install-dir DIR] [--rc FILE] [--glyphs|--no-glyphs] [--json]
sigil doctor [--shell auto|zsh|bash] [--json]
```

Examples:

```sh
sigil command "find large files"
sigil command "show modified Python files"
sigil ask "what changed in this repo?"
sigil ask --follow-up "what should I test?"
git diff | sigil ask "review risky changes"
git diff --name-only | sigil command "run the relevant tests"
sigil act show --json
sigil events --limit 50
sigil session show --json
sigil status
```

## `sigil command`

Generates one command proposal from a prompt, using the same comma (`,`)
proposal route as the glyph.

```sh
sigil command "find files over 10 MB"
sigil command "show the largest directories"
git diff --name-only | sigil command "run the relevant tests"
```

Sigil prints the proposed command to stdout, followed by a short explanation on
its own line when present.

When stdin is piped, Sigil asks before using the piped text (except with
`--json`, which is treated as a machine-mode call and skips the prompt).

JSON output:

```sh
sigil command --json "find Python tests"
```

```json
{"prompt":"find Python tests","command":"find . -name 'test_*.py' -o -name '*_test.py'","explanation":"Finds common Python test filenames."}
```

Stable fields:

- `prompt`: prompt text.
- `command`: directly runnable shell command.
- `explanation`: short explanation from the model.

## `sigil ask`

Answers a shell question using Pi. `sigil ask` uses the local read-only answer
route by default. `--follow-up` remains an explicit long-form continuation path
and uses the web-authorized answer route.

```sh
sigil ask "what is this error?"
sigil ask --follow-up "what should I try next?"
git diff | sigil ask "review this diff"
```

A fresh `sigil ask` starts a new same-session question transcript. `--follow-up`
continues that transcript.

When stdin is piped into a question, Sigil attaches it to the prompt without an
extra confirmation. Question routes have no execute path and no Bash tool. If
an answer recommends a command, it is plain answer text.

JSON output:

```sh
sigil ask --json "summarize this repository"
```

The JSON form is one object:

```json
{
  "ok": true,
  "type": "answer",
  "question": "summarize this repository",
  "prompt": "summarize this repository",
  "follow_up": false,
  "answer": "Sigil is a shell assistant...",
  "answer_event_id": "c8ad3f8e-...",
  "tools": [],
  "malformed_events": 0
}
```

Stable fields:

- `ok`: whether stream rendering completed successfully.
- `type`: currently `"answer"`.
- `question`: user-visible question text.
- `prompt`: prompt sent to Pi. Follow-ups include transcript context.
- `follow_up`: whether `--follow-up` was used.
- `answer`: concatenated assistant text.
- `answer_event_id`: stored answer event id, or `null`.
- `tools`: ordered Pi tool trace events.
- `malformed_events`: malformed Pi JSON event lines ignored.

With piped stdin and `--json`, a fresh `sigil ask` currently emits pipeline
metadata instead of calling Pi:

```sh
printf 'hello\n' | sigil ask --json "summarize"
```

```json
{
  "glyph": "?",
  "base": "?",
  "depth": 1,
  "name": "answer",
  "prompt": "summarize",
  "stdin": "hello\n",
  "mode": "pipeline"
}
```

## Shell Glyphs

Glyphs are installed shell functions over the CLI runtime. Install them with
`sigil install zsh` or `sigil install bash`.

```text
,    recommend one command
,,   run one agent turn, confirming effects
,,,  run one agent turn, auto-approving routine effects
?    answer from local read-only context
??   answer from local context plus web search
@    run a bounded goal loop with checkpoints
@@   run a bounded goal loop with routine auto-approval
```

Examples:

```sh
, find files larger than 10 MB
,, run the relevant tests
,,, fix the failing parser test
? why does git say this branch diverged?
?? what changed upstream in the latest release?
@ fix the failing parser test
@@ update docs and run checks
```

`,` prints a command proposal. The zsh binding inserts it into the editable
prompt buffer and adds it to shell history; the Bash binding adds it to history.
`,,` asks before running one Pi
agent turn with read/search/edit/write tools. A turn is one Pi invocation and
may include zero or more tool calls. `,,,` runs the same one-turn route without
routine confirmation. `@` and `@@` repeat bounded turns toward a
durable goal, stopping on completion, blockage, budget exhaustion, or
interruption. Bash tool execution is blocked and staged as a command for review.

To install bindings without glyphs:

```sh
sigil install zsh --no-glyphs
```

## `sigil act`

Inspects or controls the current one-step Pi action used by comma routes.

```sh
sigil act
sigil act show
sigil act resume
sigil act resume --verbose
sigil act abort
sigil act show --json
```

`resume` runs the pending action only after confirmation. If there is no active
action, it exits with status `2`.

Act output streams Pi's raw tool calls and prose through `glow` or `cat`.
Sigil does not replace the final answer with a compact `done:` summary for
agent steps.

JSON output for `show` is the stored act object, or `null`:

```json
{
  "act_id": "1a4e...",
  "objective": "fix the failing parser test",
  "status": "active",
  "steps": [
    {
      "id": "1",
      "title": "Run one Pi edit step",
      "command": "pi --tools read,grep,find,ls,bash,edit,write",
      "explanation": "One confirmed read/edit/write pass, then control returns to the shell.",
      "status": "pending"
    }
  ]
}
```

`abort --json` returns:

```json
{"aborted":true,"act":{ "...": "..." }}
```

## `sigil events`

Shows recent records from the read-only global event log.

```sh
sigil events
sigil events --limit 50
sigil events --json
sigil events --json --raw
sigil events list --json
```

Without `--json`, each event is printed as a table:

```text
time      id        action      session   summary
12:34:56  7c0d5a11  ? question  2e9a0b3c  what changed?
```

The summary JSON form returns:

```json
[
  {
    "id": "7c0d5a11-...",
    "short_id": "7c0d5a11",
    "time": 1760000000.0,
    "time_label": "12:34:56",
    "type": "question",
    "glyph": "?",
    "action": "? question",
    "session": "2e9a0b3c-...",
    "short_session": "2e9a0b3c",
    "cwd": "/path/to/repo",
    "summary": "what changed?"
  }
]
```

Use `--raw --json` to return exact stored event payloads.

## `sigil session`

Inspects or clears current shell-session state.

```sh
sigil session show
sigil session path
sigil session list
sigil session clear
sigil session show --json
```

`show --json` returns the current session id, session path, and parsed
continuity files:

```json
{
  "session_id": "2e9a0b3c-...",
  "path": "/Users/me/.sigil/sessions/2e9a0b3c-...",
  "files": {
    "last-question.jsonl": [],
    "last-tools.jsonl": [],
    "last-failure.json": null,
    "last-act.jsonl": [],
    "recent-turns.jsonl": []
  }
}
```

`path --json` returns:

```json
{
  "state": "/Users/me/.sigil",
  "session": "/Users/me/.sigil/sessions/2e9a0b3c-...",
  "session_id": "2e9a0b3c-...",
  "events": "/Users/me/.sigil/events.jsonl"
}
```

`list --json` returns known session directories, file names present in each
session, and latest event metadata when available. `clear --json` returns the
session files removed.

## `sigil status`

Shows the shortest useful current-session state without calling the model or
mutating session files.

```sh
sigil status
sigil status --json
```

When there is no live state that needs attention, it prints:

```text
clean
```

When attention is needed, it exits with status `1` and prints the highest
priority condition plus exact next commands. Priority is active act, pending
staged command, latest failed shell turn, then latest failed Sigil execution.

The bindings also capture bounded stdout and stderr snippets for ordinary
interactive shell turns so recovery prompts can use the actual failure output.
Disable capture with `SIGIL_ENABLE_TURN_CAPTURE=0`.

JSON output:

```json
{
  "state": "attention",
  "reason": "last command failed",
  "session_id": "2e9a0b3c-...",
  "cwd": "/path/to/repo",
  "actions": [", suggest a fix"],
  "details": {
    "command": "uv run pytest",
    "status": 1
  }
}
```

## `sigil install`

Installs or updates a shell binding and adds an idempotent source block to the
shell rc file.

```sh
sigil install zsh
sigil install bash
sigil install zsh --no-glyphs
sigil install bash --install-dir ~/.sigil/shell/bash --rc ~/.bashrc
sigil install zsh --json
```

JSON output:

```json
{
  "shell": "zsh",
  "binding_path": "/Users/me/.sigil/shell/zsh/sigil.zsh",
  "rc_path": "/Users/me/.zshrc",
  "source_path": "/path/to/package/sigil/shell/zsh/sigil.zsh",
  "wrote_rc": true,
  "glyphs_enabled": true
}
```

## `sigil doctor`

Checks whether the local install is ready.

```sh
sigil doctor
sigil doctor --shell bash
sigil doctor --json
```

Doctor checks:

- `sigil`, `glow`, and `pi` are on `PATH`.
- The model endpoint is reachable from `SIGIL_MODEL_URL`, or from the default
  local OpenAI-compatible endpoint.
- `SIGIL_MODEL_NAME` is set when the endpoint needs an explicit model name.
- Sigil's state directory is writable.
- The selected shell is supported.
- The selected shell binding is installed.
- The current environment looks like it inherited a loaded binding.

JSON output is an ordered list:

```json
[
  {
    "name": "executable:sigil",
    "status": "ok",
    "detail": "/Users/me/.local/bin/sigil",
    "hint": null
  }
]
```

`doctor` exits nonzero when any check has `status: "fail"`. Warnings do not
change the exit code.

## State and Environment

By default, Sigil writes state under `~/.sigil/`.

```text
events.jsonl                              global event log
sessions/<session-id>/last-failure.json   latest failed shell command
sessions/<session-id>/last-act.jsonl      one-step Pi agent action snapshots
sessions/<session-id>/last-goal.jsonl     bounded goal loop snapshots
sessions/<session-id>/last-question.jsonl same-session question transcript
sessions/<session-id>/last-staged-command.jsonl latest blocked command staged for review
sessions/<session-id>/last-tools.jsonl    latest Pi tool trace
sessions/<session-id>/recent-turns.jsonl  recent shell turns recorded by bindings
```

Environment variables:

```sh
SIGIL_STATE_DIR=/custom/state/root
SIGIL_SESSION_ID=my-shell-session
SIGIL_SESSION_DIR=/custom/session/root
SIGIL_ENABLE_GLYPHS=0
SIGIL_ENABLE_TURN_CAPTURE=0
SIGIL_TURN_CAPTURE_BYTES=6000
SIGIL_BIN=/path/to/sigil
SIGIL_GLOW_STYLE=notty
SIGIL_GLOW_WIDTH=88
SIGIL_MODEL_URL=http://127.0.0.1:8080/v1/chat/completions
SIGIL_MODEL_NAME=local-model
SIGIL_MODEL_PATH=/path/to/model.gguf
```
