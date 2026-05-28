# Make TUIs Obsolete

Sigil should not become an agent dashboard. The goal is to make the shell feel
like it has the missing agent verbs: propose, execute one bounded action, ask,
recover, inspect state, and audit history.

The product constraint is strict:

- No persistent agent screen.
- No inbox.
- No dashboard.
- No hidden agent place.
- State appears only through the shell buffer, command output, `sigil status`,
  `sigil events`, and future audit commands.

## Target Experience

A complete flow should feel inevitable:

```sh
uv run pytest
, fix
,, run focused test
? explain the risk
git commit -m "..."
sigil status
```

The user should not think "I need an agent TUI." They should think "my shell
already has the agent affordances I need."

## Ladder 1: Ambient State

Goal: the shell tells you whether Sigil has live state without opening anything.

Add prompt integration over `sigil status`:

```text
$      clean
! $    attention
```

`sigil status` remains the explanation path. It should stay cheap: no model
call, no network, no doctor checks, no mutation.

Initial attention reasons:

- active act
- pending bash handoff
- latest failed shell turn
- latest failed Sigil action

Definition of done: after any command, the prompt can tell whether `sigil
status` is worth running.

## Ladder 2: Recovery Loop

Goal: failed command to useful next action in one gesture.

Make these work reliably:

```sh
, fix
? why failed
```

They should consume:

- latest command
- exit status
- cwd
- bounded stderr/stdout
- recent shell turns
- relevant git status
- recent question context

Add secret hygiene before capturing command output:

- skip leading-space commands
- redact common token and environment patterns
- bound captured output aggressively

Definition of done: after `uv run pytest` fails, `, fix` produces a focused next
command or concise explanation without extra user context.

## Ladder 3: Command Trust Labels

Goal: every proposal says what kind of action it is.

Examples:

```text
uv run pytest tests/test_status.py
no risk labels
```

```text
git push origin main
network · publish
```

Implementation direction:

- Extend policy classification into user-facing labels.
- Keep labels terse.
- Make `,,` confirmation conditional on risk.
- Record labels into events.

Definition of done: users can judge a proposed command in one glance.

## Ladder 4: `sigil why`

Goal: inline audit context for the last meaningful Sigil output.

```sh
sigil why
```

It should explain:

- what command, answer, or action it refers to
- what context was used
- model route used
- trust mode and risk labels
- inherited inputs
- why this action was selected
- exact lineage command for deeper audit

This is not a verbose trace dump. It should be a readable explanation over
existing events.

Definition of done: after `,`, `,,`, `?`, or `,,,`, `sigil why` explains the
last meaningful Sigil output.

## Ladder 5: First-Run Clarity

Goal: setup failures are actionable.

Improve `sigil doctor` so every failure includes the exact next command when
there is a safe obvious fix. Warnings should stay warnings, but they should
still tell users what to do next.

Definition of done: a new user can run `sigil doctor` and fix setup without
reading docs first.

## Execution Order

1. Prompt marker over `sigil status`.
2. Capture bounded stdout and stderr for recent turns.
3. Improve `, fix` and failure-context prompting.
4. Add risk labels to proposals and events.
5. Add `sigil why`.
6. Polish first-run and doctor output with exact fix commands.
7. Record a killer demo flow and use it as regression material.

## TODO

### Ambient State

- [x] Add a cheap machine-readable `sigil status --json` call path for shell
      bindings.
- [x] Add zsh prompt integration that shows `!` when status is `attention`.
- [x] Add Bash prompt integration with the same marker behavior.
- [x] Ensure prompt integration never calls the model or network.
- [x] Add tests for clean and attention prompt states.
- [x] Document how to disable the prompt marker.

### Recovery Loop

- [x] Extend recent turn records with bounded stdout and stderr snippets when
      the shell provides them.
- [x] Keep leading-space commands out of captured turn state.
- [x] Add redaction for common token, key, password, and bearer patterns.
- [x] Preserve prompt responsiveness while recording richer turn state.
- [x] Update failure-context prompts to prefer recent turn output when present.
- [x] Add fixtures for common failures: pytest, missing command, git, network,
      and permission errors.
- [x] Make `, fix` work as a first-class phrase in tests.
- [x] Make `? why failed` explain the last failure without asking for more
      context.
- [x] Capture bounded stdout and stderr automatically for ordinary shell turns,
      beyond the existing `SIGIL_FAILURE_STDOUT` and `SIGIL_FAILURE_STDERR`
      hook point.
- [x] Add a deterministic demo for `, fix` and `? why failed`.

### Command Trust Labels

- [x] Define a small risk label vocabulary: network, publish, delete,
      privileged.
- [x] Map existing policy classifications to labels.
- [x] Print labels under `,` proposals.
- [x] Record labels in operator events.
- [x] Make `,,` require explicit confirmation before generated commands become
      actions.
- [x] Add tests for read-only, write, network, delete, and privileged commands.

### `sigil why`

- [ ] Add a `sigil why [EVENT_ID] [--json]` command.
- [ ] Default to the latest meaningful Sigil event in the current session.
- [ ] Explain the selected output, context inputs, trust label, route, and model
      source.
- [ ] Include the exact `sigil events lineage ...` command for deeper audit.
- [ ] Keep human output short and non-trace-like.
- [ ] Add tests for `,`, `,,`, `?`, and `,,,` audit context.

### First-Run Clarity

- [ ] Add exact fix commands or next steps to `doctor` failures.
- [ ] Keep warnings non-fatal, but make their remediation explicit.
- [ ] Add JSON fields for remediation commands.
- [ ] Add tests for missing `pi`, missing `glow`, unreachable model endpoint,
      missing model name, and unwritable state directory.

### Demo And Regression

- [ ] Create a deterministic demo for:
      `uv run pytest` -> `, fix` -> `,, run focused test` -> `? explain the
      risk` -> `sigil status`.
- [ ] Use the demo fixtures as regression tests where practical.
- [ ] Update README examples around the final flow.
- [ ] Re-rate the CLI against the 10/10 criteria after the demo passes.
