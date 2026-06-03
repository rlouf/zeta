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

`sigil status` is the explanation path: run it to see live state on demand. It
should stay cheap: no model call, no network, no doctor checks, no mutation.

Initial attention reasons:

- active act
- pending staged command
- latest failed shell turn
- latest failed Sigil action

Definition of done: after any command, the prompt can tell whether `sigil
status` is worth running.

## Ladder 2: Recovery Loop

Goal: failed command to useful next action in one gesture.

Make these work reliably:

```sh
, fix
, why failed
```

They should consume:

- latest command
- exit status
- cwd
- bounded stderr/stdout
- recent shell turns
- relevant git status
- recent answer context

Add secret hygiene before capturing command output:

- skip leading-space commands
- redact common token and environment patterns
- bound captured output aggressively

Definition of done: after `uv run pytest` fails, `, fix` produces a focused next
command or concise explanation without extra user context.

## Ladder 3: `sigil why`

Goal: inline audit context for the last meaningful Sigil output.

```sh
sigil why
```

It should explain:

- what command, answer, or action it refers to
- what context was used
- model route used
- why this action was selected

This is not a verbose trace dump. It should be a readable explanation over
existing events.

Definition of done: after `,`, `,,`, `?`, or `,,,`, `sigil why` explains the
last meaningful Sigil output.

## Ladder 4: First-Run Clarity

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
4. Add `sigil why`.
5. Polish first-run and doctor output with exact fix commands.
6. Record a killer demo flow and use it as regression material.

## TODO

### Ambient State

- [x] Add a cheap machine-readable `sigil status --json` call path.
- [x] Keep `sigil status` cheap: never call the model or network.

### Recovery Loop

- [x] Extend recent turn records with bounded stdout and stderr snippets when
      the shell provides them.
- [x] Keep leading-space commands out of captured turn state.
- [x] Add redaction for common token, key, password, and bearer patterns.
- [x] Preserve prompt responsiveness while recording richer turn state.
- [x] Update failure-context prompts to prefer recent turn output when present.
- [x] Add fixtures for common failures: pytest, missing command, git, network,
      and permission errors.
- [x] Attach the last failure to `,` and `?` whenever it is the latest shell
      turn, regardless of how the prompt is phrased.
- [x] Make `, why failed` explain the last failure without asking for more
      context.
- [x] Capture bounded stdout and stderr automatically for ordinary shell turns,
      beyond the existing `SIGIL_FAILURE_STDOUT` and `SIGIL_FAILURE_STDERR`
      hook point.
- [x] Add a deterministic demo for `, fix` and `, why failed`.

### `sigil why`

- [ ] Add a `sigil why [EVENT_ID] [--json]` command.
- [ ] Default to the latest meaningful Sigil event in the current session.
- [ ] Explain the selected output, context inputs, route, and model source.
- [ ] Keep human output short and non-trace-like.
- [ ] Add tests for `,`, `,,`, `?`, and `,,,` audit context.

### First-Run Clarity

- [ ] Add exact fix commands or next steps to `doctor` failures.
- [ ] Keep warnings non-fatal, but make their remediation explicit.
- [ ] Add JSON fields for remediation commands.
- [ ] Add tests for missing `zeta`, missing `glow`, unreachable model endpoint,
      missing model name, and unwritable state directory.

### Demo And Regression

- [ ] Create a deterministic demo for:
      `uv run pytest` -> `, fix` -> `,, run focused test` -> `? explain the
      risk` -> `sigil status`.
- [ ] Use the demo fixtures as regression tests where practical.
- [ ] Update README examples around the final flow.
- [ ] Re-rate the CLI against the 10/10 criteria after the demo passes.
