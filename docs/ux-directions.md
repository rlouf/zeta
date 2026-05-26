# Sigil UX directions

Sigil should help with terminal work that is stateful, easy to get subtly
wrong, or annoying to reconstruct from memory.

Unlike agent TUIs that own the loop and call shell tools from inside an agent
session, Sigil starts from the shell. The shell remains the place where commands
are inspected, edited, and executed. Sigil should cross the agent/shell boundary
only through explicit glyph semantics: `,` recommends, and `,,` executes.

The core trust boundary is:

```text
agent recommends
double comma executes
```

This makes handoff quality the central UX problem. Sigil should mostly explain,
repair, summarize, search, and recommend concrete next actions. Direct execution
should stay rare and explicit.

## Command repair

After a command fails, Sigil should be able to inspect the last command, stderr,
exit status, cwd, and relevant project files, then propose a repair.

Possible interaction:

```text
?!          explain the last failure and suggest a fix
?! retry    suggest a corrected command for the last failure
```

The output should be concise:

- what failed
- why it likely failed
- one or more corrected commands
- any risk or assumption worth checking before retrying

Repairs should prefer visible previews over running automatically. If the fix is
destructive, Sigil should show a dry run or a verification command first.

## Project conventions through skills

Sigil should learn local project conventions before suggesting commands. This
could be modeled as lightweight skills: small convention packs discovered from
the repo, user config, or explicit files.

Useful convention sources:

- `AGENTS.md`, `CLAUDE.md`, `README.md`, and local docs
- `package.json`, `pyproject.toml`, `Cargo.toml`, `Makefile`, `justfile`
- existing shell history and Sigil event history
- user-defined skills under a Sigil config directory

Examples:

```text
, run the relevant tests
, lint what I changed
, start the app
, make a commit message
```

Sigil should use conventions to choose the right local command shape, not to add
a large planning layer. A good result is boring and specific: the command the
project already expects people to run.

## Session summary and past memory

The event log should become a useful terminal memory layer. Sigil can summarize
the current session and search past sessions without requiring the user to
manually reconstruct context.

Possible interactions:

```text
@@ query    search past Sigil memory
```

Session summaries should focus on operational state:

- commands generated or selected
- questions asked and useful answers
- failed commands and repairs
- files, branches, servers, or directories that mattered
- open follow-ups or unresolved errors

Past memory search should answer questions like:

```text
@@ how did I run the release script last time?
@@ what command converted mov files to mp4?
@@ when did I debug port 3000?
```

Memory should be transparent. Sigil should show the source session, cwd, and
timestamp for retrieved entries, and avoid treating stale history as current
truth without checking local state.
