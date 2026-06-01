# zsh-native Zeta Loop

This proposal describes a rewrite path where Zeta's main agent loop moves into
zsh and tools become ordinary CLI commands. The target is not a TUI and not an
agent subprocess that happens to launch shells. The target is an agent turn that
participates in the user's current shell session.

Sigil already treats the shell as the review boundary. Previous designs put the
agent loop inside an external agent CLI and exposed shell integration as a
bridge. This design inverts that relationship:

- zsh owns the interactive loop, prompt, history, `$PWD`, environment, job
  control, completion, and handoff.
- the model is called by shell functions or small helper CLIs.
- tools are executables with a stable JSON contract.
- shell execution is not a hidden tool call; it is staged into the editable zsh
  prompt.

## Naming And Boundary

Zeta is the proposed agent loop/runtime, not a rename of Sigil.

The intended split is:

- Sigil owns the shell product surface: glyphs, install flow, status/events,
  demos, and user-facing workflows.
- Zeta owns the runtime services: model transport, tool discovery, tool protocol,
  effect analysis, transcript format, and shell handoff semantics.

This keeps the migration reversible. Sigil can embed Zeta as its runtime, while
Zeta can later be decoupled into a standalone CLI or reused by another shell
surface without carrying Sigil's glyphs and product assumptions.

## Runtime Ownership

The Sigil/Zeta runtime path is conceptually split into:

- The Sigil zsh loop, which owns agent control flow and the user-facing shell
  entrypoint.
- The Zeta CLI, which provides model transport, tool implementations, analysis
  helpers, and storage operations.

The Sigil zsh loop is the authority over the next step. It decides when to call
the model, when to invoke tools, when to suspend for handoff, when to resume, and
when a turn is complete.

The CLI does not own the agent loop. It provides operations the loop can call.
This distinction is intentional. Previous designs embedded the step loop inside
a CLI process and exposed shell integration as a bridge. Zeta inverts that
relationship: the shell owns the loop and calls the CLI.

The defining architectural choice is not that Zeta uses zsh. The defining choice
is that the shell owns the agent control loop. Zeta's model transport, storage,
and tool implementations may evolve independently, but the Sigil shell loop
remains the authority over when the next agent step occurs.

## Goals

- Make Sigil feel like an agent inside the user's shell, not a separate app
  controlling it.
- Keep tools language-agnostic and easy to replace.
- Preserve inspectability: every mutation has either a preview, a confirmation,
  or a handoff into the prompt.
- Keep non-interactive tools boring enough to write in shell, Python, Rust,
  Node, or any other language.
- Allow a minimal prototype without replacing every existing Zeta capability.

## Non-Goals

- Do not build a persistent dashboard, chat pane, or TUI.
- Do not intercept ordinary user commands.
- Do not make zsh parse or validate arbitrary tool-specific payloads.
- Do not require every tool to be written in shell.
- Do not make hidden shell execution the default agent escape hatch.

## V1 Scope

V1 includes:

- Sigil zsh control loop for one agent turn.
- `zeta model stream`.
- `zeta tools list --json`.
- `zeta tool read`.
- `zeta tool grep`.
- `zeta tool bash`.
- `zeta tool edit`.
- `zeta tool write`.
- `zeta transcript append` and `zeta transcript tail`.
- `--json`, `--schema`, and `--analyze` for built-in tools.
- Prompt handoff with `print -z` for bash, edit, and write.

V1 excludes:

- Policy gates and custom policy profiles.
- Project-local tool trust.
- Approval ledgers.
- Auto-continue.
- Goal loops.

## Command Surface

```text
sigil                     # shell product entrypoint and zsh control loop
zeta model stream         # model transport and normalized stream events
zeta tools list --json    # merged tool registry
zeta tool read            # read files
zeta tool grep            # search text
zeta tool bash            # interactive command handoff
zeta tool edit            # patch artifact + staged apply handoff
zeta tool write           # file artifact + staged write handoff
zeta transcript append
zeta transcript tail
```

`zeta transcript append` reads one event from stdin and appends it to the active
transcript. `zeta transcript tail` returns recent events for the next model
request.

The `sigil` entrypoint is a zsh function or script sourced into the current
shell, so it can use ZLE and mutate the prompt buffer. Running inside the live
shell is what makes the handoff possible: the loop stages commands into the same
prompt the user is typing at.

The Zeta CLI is the canonical implementation of built-in tools and services.
External tools may still be discovered through executable plugins for
extensibility, but built-in capabilities are exposed as subcommands rather than
as a pile of separate binaries.

## Tool Discovery

Discovery should be deterministic and hierarchical:

1. Built-in tools exposed by `zeta tool`.
2. External `zeta-tool-`-prefixed executables on `$PATH`.
3. Optional project-local tools from `.zeta/tools`, only when explicitly enabled.

`zeta tools list --json` returns a merged view of built-in and external tools.
Only external executables whose names start with `zeta-tool-` are discovered as
tools; ordinary commands on `$PATH` are never silently exposed to the model. For
external tools, the model-facing name strips that prefix:

```text
zeta-tool-read  -> read
zeta-tool-grep  -> grep
zeta-tool-bash  -> bash
```

Built-ins win over external tools of the same name. External tools on `$PATH` win
over project-local tools. The discovery result is captured at the start of an
agent turn and written into the transcript so a turn can be audited later.

Recommended discovery command:

```sh
zeta tools list --json
```

Example output:

```json
{
  "tools": [
    {
      "name": "read",
      "command": ["zeta", "tool", "read"],
      "origin": "builtin",
      "schema": { "type": "object" }
    }
  ]
}
```

Project-local `.zeta/tools` is intentionally not enabled by default. It is useful
for repo-specific workflows, but it means untrusted checkouts can define agent
capabilities. An explicit `ZETA_ENABLE_PROJECT_TOOLS=1` or equivalent Sigil
route flag keeps that boundary visible.

## Tool Protocol

Each tool invocation, whether a built-in `zeta tool NAME` subcommand or an
external `zeta-tool-NAME` executable, has this runtime contract:

```text
stdin   JSON input
stdout  JSON result
stderr  human/debug logs
exit    0 on success, non-zero on failure
```

Each tool must support:

```sh
tool --schema       # JSON Schema for model input
tool --analyze      # JSON input -> JSON effect analysis, no mutation
tool --help         # human help
tool --json         # machine-readable tool metadata
```

For built-ins, `tool` means a concrete subcommand such as `zeta tool read`. For
external plugins, it means the executable such as `zeta-tool-read`.

`--json` is the canonical source of a tool's metadata. `--schema` returns the same
schema value `--json` reports under `schema`, and `zeta tools list --json`
aggregates `--json` across discovered tools. None is authored independently, so
they cannot drift.

`--json` should include at least:

```json
{
  "name": "read",
  "description": "Read a UTF-8 text file from the current workspace.",
  "schema": {
    "type": "object",
    "additionalProperties": false,
    "required": ["path"],
    "properties": {
      "path": { "type": "string" },
      "offset": { "type": "integer", "minimum": 0 },
      "limit": { "type": "integer", "minimum": 1 }
    }
  },
  "security": {
    "analyzer": "self",
    "analysis_schema": "zeta.analysis.v1"
  },
  "interactive": false
}
```

`--analyze` receives the same JSON input as normal tool execution and returns a
side-effect-free prediction of what the tool would do. In v1, the Sigil loop
records this analysis for audit and uses fixed route behavior: read/search tools
run, while `bash` handoffs are staged into the prompt and never executed by the
loop.

For the first version, discovered executables are trusted to report their own
analysis. `analyzer: "self"` means "this tool analyzes itself." That is not a
complete tool-trust model, but it is the pragmatic starting point for a
shell-native tool system: if a tool is installed and executable, Zeta assumes its
`--analyze` and invocation behavior belong to the same trusted unit.

This can become stricter later. The initial design should keep the protocol
simple and make the analysis path universal before adding policy gates.

Example analysis:

```json
{
  "valid": true,
  "resolved": true,
  "effects": [
    {
      "kind": "read",
      "resource": "path",
      "target": "README.md",
      "certainty": "certain"
    }
  ],
  "diagnostics": []
}
```

Tool result JSON uses a common envelope:

```json
{
  "ok": true,
  "content": [
    { "type": "text", "text": "..." }
  ],
  "metadata": {
    "path": "README.md"
  }
}
```

On failure, tools should return non-zero and still write a JSON error envelope
to stdout when practical:

```json
{
  "ok": false,
  "error": {
    "code": "not_found",
    "message": "README.md does not exist"
  }
}
```

stderr remains for logs meant for the human. The Sigil loop may show or
suppress stderr depending on verbosity, but it should record bounded stderr in
the transcript for audit.

## JSON Schema Format

Use plain JSON Schema draft 2020-12 for tool inputs. Avoid TypeScript-specific,
Python-specific, or OpenAPI-only extensions in the protocol. The tool metadata
may include Zeta-specific hints, but model input validation should work with a
generic JSON Schema validator.

Recommended top-level metadata fields:

```json
{
  "name": "edit",
  "description": "Apply a patch after showing a diff.",
  "schema": {},
  "security": {
    "analyzer": "self",
    "analysis_schema": "zeta.analysis.v1"
  },
  "interactive": true,
  "idempotent": false
}
```

## Effect Analysis

Zeta's security model should be baked into the tool protocol, not bolted onto
the shell runner later. In v1 this means every tool can produce a structured,
side-effect-free analysis of what it would do. The useful foundation already
exists in `../cli`: a declarative command spec predicts concrete effects.

V1 does not need configurable policy gates. The Sigil route itself defines the
behavioral boundary: read/search tools can run, and shell execution goes through
prompt handoff. The analysis is still recorded so the system has an audit trail
and a stable protocol for adding policy gates later.

The model is:

```text
proposed tool call
-> side-effect-free analysis
-> record analysis
-> fixed route behavior: run read/search, hand off bash, or reject unsupported
```

Every effect should carry:

```text
kind       read | write | delete | execute
resource   path | url | repo | process | session
target     concrete target when known
certainty  certain | uncertain
```

An analysis has two independent verdicts:

```text
valid      would the operation run at all?
resolved   did the analyzer fully model the operation?
```

The loop can trust the effect list only when the operation is both valid and
resolved. In v1, unresolved analysis is recorded and surfaced, but it does not
drive a configurable policy decision. Route behavior stays simple: `bash` is
handoff-only regardless of the analysis, and read/search tools are the only
non-mutating tools in the prototype.

Effects are not independent: a `write` to a location that is later sourced or
executed is a deferred `execute`. A file written to `~/.zshrc`, `.git/hooks/*`, a
`Makefile`, or any directory on `$PATH` runs the next time the shell, git, or the
user invokes it — bypassing the rule that shell execution must be a user action.
The analyzer should tag a write whose normalized target is an executable/sourced
location with an additional `execute` effect. This is the "write is a universal
solvent" problem named in `../cli`. In v1 this is recorded for audit; it does not
drive a policy gate.

The security layer should use normalized targets, not raw operands. Path effects
need workspace-relative canonical paths, symlink handling, and an explicit
outside-workspace classification before policy rules such as `src/*` or
`out/*` can be trusted.

For v1, executables discovered through the enabled tool paths are
trusted by default. That means Zeta trusts a tool's `--analyze` output in the
same way it trusts the tool's invocation behavior. This is intentionally simple:
the initial goal is to make analysis universal and auditable, not to solve every
tool-trust problem.

Later versions can add install-time trust prompts, per-project trust files, or
runner-owned specs for tools whose self-analysis should not be accepted. Those
are compatible extensions, not first-version requirements.

Later versions can add `zeta policy decide --policy <name>` as a service that
turns analyses into `allow` / `confirm` / `deny` decisions. That should happen
only once real profiles and customization are needed.

## Session Transcript

For the Sigil-embedded v1, the transcript is append-only JSONL under the active
Sigil session directory:

```text
${SIGIL_SESSION_DIR:-${SIGIL_STATE_DIR:-$HOME/.sigil}/sessions/$SIGIL_SESSION_ID}/zeta-transcript.jsonl
```

The shell binding sets `SIGIL_SESSION_ID` once per shell process. Tools launched
from the shell inherit it. A standalone Zeta distribution can later map the same
format onto `ZETA_HOME`, but v1 should reuse Sigil's existing session machinery.

Recommended event shapes:

```json
{
  "type": "user_message",
  "time": "2026-06-01T10:00:00Z",
  "cwd": "/repo",
  "content": "run the focused tests"
}
```

```json
{
  "type": "tool_call",
  "time": "2026-06-01T10:00:01Z",
  "tool_call_id": "call_123",
  "name": "grep",
  "input": { "pattern": "pytest", "path": "." }
}
```

```json
{
  "type": "tool_analysis",
  "time": "2026-06-01T10:00:01Z",
  "tool_call_id": "call_123",
  "analysis": {
    "valid": true,
    "resolved": true,
    "effects": [
      {
        "kind": "read",
        "resource": "path",
        "target": "README.md",
        "certainty": "certain"
      }
    ]
  }
}
```

```json
{
  "type": "tool_result",
  "time": "2026-06-01T10:00:01Z",
  "tool_call_id": "call_123",
  "exit_code": 0,
  "result": { "ok": true, "content": [] },
  "stderr_tail": ""
}
```

```json
{
  "type": "assistant_message",
  "time": "2026-06-01T10:00:02Z",
  "content": "The focused command is `uv run pytest tests/test_cli.py`."
}
```

The transcript is the source of truth for `zeta status`, `zeta why`, and later
continuation. It should store:

- user prompt and selected mode
- `$PWD` and selected environment facts
- discovered tools and schemas
- model request metadata, excluding secrets
- streamed assistant content
- tool calls, bounded outputs, and exit codes
- tool analyses
- user approvals, edits, declines, and interrupts

## Model Loop

The Sigil zsh loop is the runtime control loop. The loop owns agent control flow.
Helper commands may provide model, analysis, or storage operations, but
they never decide the next step.

For one turn the loop:

1. Read the user objective from argv, stdin, or an interactive prompt.
2. Discover tools and schemas.
3. Build the model request from the system prompt, transcript tail, current
   shell context, and available tools.
4. Stream model output.
5. When the model requests a tool, assign the `tool_call_id` and ask the matching
   tool command to analyze the proposed JSON input without mutating anything. The
   loop owns the id: it adopts the provider's id when `zeta model stream` supplies
   one and allocates its own otherwise, so analysis and result events can
   reference the same id before the tool is ever invoked.
6. Record the analysis.
7. Apply fixed v1 route behavior: run read/search tools, stage bash through
   prompt handoff, and reject unsupported mutation paths.
8. Append tool result events to the transcript.
9. Continue until the model returns a final message, the user interrupts, or a
   budget boundary is reached.

The Sigil shell loop should not need to understand a tool's internal semantics. It
routes JSON, records analyses, and applies route-level behavior over the common
effect model.

## CLI Services

The Zeta CLI exposes reusable services to the Sigil zsh loop. Examples include:

```text
zeta model stream
zeta transcript append
zeta transcript tail
zeta tool read
zeta tool grep
```

These commands are implementation helpers, not alternate runtimes. The Sigil zsh
loop remains the authority over control flow.

## Model Transport

Model transport is provided by:

```text
zeta model stream
```

That keeps HTTP, auth, provider quirks, retries, and streaming JSON parsing out
of zsh. The Sigil zsh loop calls it and receives normalized stream events:

```json
{ "type": "assistant_delta", "text": "..." }
{ "type": "tool_call", "id": "call_123", "name": "read", "input": {} }
{ "type": "final" }
```

This preserves the principle that the loop is zsh without forcing zsh to be the
HTTP client and streaming parser.

## Bash Handoff

`zeta tool bash` is part of v1. It does not execute shell commands. It analyzes
the proposed command, records the analysis, and returns a handoff request so the
Sigil zsh loop can stage the command into the editable prompt.

For simple commands, `zeta tool bash --analyze` should parse the command into
argv, look up a trusted command spec, and return predicted effects. For shell
grammar such as pipes, redirects, command substitution, backticks, `&&`, `;`,
environment assignments, or glob expansion, it should mark the analysis
unresolved. The shell is too expressive to silently classify as safe.

Unknown commands should also be unresolved. In v1 this does not trigger a policy
gate; it is recorded and the command still goes through prompt handoff, never
hidden execution.

`zeta tool bash` does not run the command. On invocation, it returns a handoff
request:

```json
{
  "ok": true,
  "handoff": {
    "type": "shell_prompt",
    "command": "uv run pytest tests/test_cli.py",
    "reason": "Run the focused tests for the files inspected above."
  }
}
```

Every shell command remains a user action, not hidden agent execution. The prompt
buffer is the mutation boundary: the agent proposes, the human runs.

## Mutation Handoff

V1 uses one mutation model for bash, edit, and write: stage a command into the
user's prompt and let the user run it.

The unifying observation is that a write is a shell operation in disguise: writing
a file is equivalent in power to a redirect, and writing an executable or sourced
file is a deferred command. So the safe model for writes and edits is the same as
the v1 bash handoff: stage a command, let the user run it.

### Writes And Edits

Writes and edits hand off the same way, but with one rule: **the content goes to a
temp artifact, and only a small, analyzable command is staged.** The naive
alternative — inlining content into the prompt with a here-doc such as
`cat > ~/.zshrc <<'EOF' … EOF` — fails on three things at once: arbitrary file
content lands in the ZLE buffer (unusable past a few lines, hopeless for binary);
a here-doc is exactly the redirect grammar the analyzer must mark *unresolved*, so
the one operation you most want to analyze becomes un-analyzable; and delimiter
collisions and `$(...)`/backtick quoting become a correctness hazard.

Instead, `zeta tool edit` writes the patch to a temp file, shows the diff in the
terminal (the actually-reviewable artifact), and stages an `apply` command:

```json
{
  "ok": true,
  "handoff": {
    "type": "shell_prompt",
    "command": "git apply /tmp/zeta-edit-7f3a.patch",
    "reason": "Apply the one-line fix to src/sigil/cli/ask.py shown above.",
    "artifact": "/tmp/zeta-edit-7f3a.patch"
  }
}
```

The prompt buffer stays one line, the staged `git apply PATH` is clean argv, and
the analyzer can resolve its effects by reading the patch artifact and extracting
the paths named in the diff. The diff is shown where the user can read it, and
the full content lives in a temp file the user can open or modify. If the user
modifies the artifact before running the staged command, that modified artifact
is the source of truth: the user has taken over the operation, and continuation
records what actually ran.

Plain `git apply` does not require a git repository — it patches the working tree
like `patch(1)`, so it works in any directory (only `--index`, `--cached`, and
`--3way` need a repo). It also covers new-file creation via a `/dev/null` diff.
The natural split is by operation, not by repo: `zeta tool edit` stages
`git apply` for diffs against existing content, while `zeta tool write` stages
`cp /tmp/zeta-write-7f3a /dest` for whole-file create/overwrite, where a diff
would be noise.

This is full uniformity: even an in-workspace `src/foo.py` edit is staged rather
than applied directly. Direct apply after diff-confirm can be considered later,
but the first version keeps one mutation path: handoff.

### Loop Behavior

When the Sigil zsh loop sees a handoff result, after recording the analysis, it
should:

1. Print the reason and proposed command (and diff, for edits/writes).
2. Print the predicted effects when available.
3. Put the command into the editable prompt buffer.
4. Return control to ZLE so the user can edit, run, or reject it.
5. Suspend the agent step pending the user's action.

### Continuing After A Handoff

Handoff suspends the step rather than ending it outright. A staged command is a
pending tool call whose result is not yet known; once the user runs it, that
result exists and the step *can* resume — but only if the user chooses to.

The two consents stay separate and both belong to the user: the user runs the
command in their own shell (consent to execute), and the user says `continue`
(consent to feed the result back). Because continuation is explicit, capturing
the command's output is not the hidden-execution path the handoff model exists to
avoid — nothing flows back into the model without the user asking for it.

Every command the user executes before `continue` is treated as a bash tool call
for the suspended step. If the user runs the staged command exactly, that result
becomes the result of the pending `bash`/`edit`/`write` call. If the user edits
the command, runs a different command, or runs several commands, Zeta records the
actual command lines, exit statuses, cwd, and bounded stdout/stderr as the tool
results supplied on continuation. The shell history is the source of truth, not
the original proposal.

`continue` resumes the step with those recorded results supplied to the model.
Doing nothing, or typing a fresh objective, abandons the suspended step.

Whether to continue depends on what was emitted: a passing command may complete
the objective and warrant stopping, while a failure may be exactly what the agent
should see and act on. That decision is the user's by default; a profile may later
automate it (e.g. auto-continue on non-zero exit for a goal route), but the
mechanism is the same explicit, recorded continuation.

In interactive zsh, prompt insertion can use:

```zsh
print -z -- "$command"
print -s -- "$command"
```

For a richer sourced function, `vared` or ZLE widgets can allow inline editing
before accepting the command. The first prototype should prefer `print -z`
because it is simple, familiar, and keeps final execution unmistakably in the
user's shell. Because content is always staged as a temp-file `apply` command,
multiline handoff needs no special buffer handling: the command itself is one
line, and the artifact is read from disk.

## Future Policy Gates

V1 does not need a configurable policy service. The first implementation should
use fixed route behavior:

```text
read/search tools          run directly
bash                       handoff only
edit/write                 handoff only
network                    out of the first prototype
```

Later, Zeta can add policy gates over the same analysis protocol:

```text
zeta policy decide --policy <name>
```

That service would read a `zeta.analysis.v1` object from stdin and return a
decision. It is deliberately not part of the v1 API surface.

## Packaging

Package the Sigil zsh loop and Zeta CLI services together, but keep the service
contract stable. In the Sigil-embedded v1, packaging exposes two console
commands from the same Python package:

```text
sigil     # user-facing product CLI and shell install entrypoint
zeta      # service CLI called by the sourced shell loop
```

Optional external plugins can still live under a later tool directory such as:

```text
~/.zeta/tools/zeta-tool-*
```

Sigil's existing shell integration defines the user-facing `sigil` function and
owns the control loop. The `zeta` command implements services the loop calls:

```text
model
tools
tool
session
transcript
```

When the user calls Sigil's agent route, the Sigil zsh function runs the control
loop itself. When that loop needs a service-shaped command such as
`zeta tools list --json` or `zeta model stream`, it delegates to `zeta`.

Prompt-buffer handoff continues to come from Sigil's shell integration:

```zsh
source "$HOME/.sigil/shell/zsh/sigil.zsh"
```

Non-zsh shells can use the same CLI services, but they will need
different handoff mechanics. The zsh path should be the reference
implementation rather than the lowest common denominator.

## Minimal Prototype

The smallest prototype that proves the design:

1. A Sigil zsh route that accepts one objective and runs the control loop.
2. Built-in tool discovery from `zeta tools list --json`.
3. `zeta tool read`, `zeta tool grep`, `zeta tool bash`, `zeta tool edit`, and
   `zeta tool write`.
4. `zeta model stream` normalizing one provider's streaming tool-call events.
5. `zeta tool bash --analyze` backed by a small effect analyzer that marks shell
   grammar unresolved.
6. `zeta tool edit` and `zeta tool write` staged through temp artifacts.
7. JSONL transcript recording of tool calls, analyses, and results.
8. Prompt handoff with `print -z`.

A useful first demo is:

```sh
,, find the focused test command for my current changes
# model calls read/grep
# model calls bash with the proposed test command
# zeta records the bash analysis
# zsh inserts the command into the editable prompt
```

Second slice:

```sh
,, apply the smallest fix for the failing test
# model reads/searches
# model calls edit
# edit writes a temp patch artifact
# zsh inserts the git apply command into the editable prompt
```

## Migration From Current Sigil

Current Sigil can migrate in stages:

1. Keep existing glyphs and CLI routes.
2. Add `zeta` service subcommands around existing read/search/session behavior.
3. Add the effect-analysis protocol to built-in `zeta tool` subcommands.
4. Replace the current TypeScript `sigil_shell` bridge with `zeta tool bash`
   handoff for the zsh route.
5. Move one-step comma routes to the zsh-native loop.
6. Move goal routes only after transcript, budget, and interruption behavior is
   stable.

During the transition, Python can still own storage helpers, model helpers, and
tool implementations. The architectural change is that zsh owns the agent loop
and the shell handoff, not that every line must be rewritten in shell.

## Open Questions

- What is the exact `zeta.analysis.v1` JSON schema, and how closely should it
  mirror the `../cli` dataclasses?
- How much shell grammar should `zeta tool bash` reject outright versus mark
  unresolved and hand off with confirmation?
- Should the transcript include full tool stdout by default, or only bounded
  tails plus artifact paths?
- What is the right normalized streaming event format between `zeta model stream`
  and the Sigil zsh loop?
- Handoff suspends the step and an explicit `continue` resumes it with every
  command the user ran in between. What is the right surface for `continue` — a
  glyph, a `sigil continue` command, a ZLE widget — and how long does a suspended
  step stay resumable (until the next prompt, until a new objective, a TTL)?
- Which existing Sigil events should be preserved verbatim for compatibility
  with `sigil events` and future `sigil why`?

Post-v1 questions:

- Should project-local tools require a per-repo trust file, an environment
  variable, or both?
- Where should approval ledgers live, and which repeated prompts are worth
  remembering?
