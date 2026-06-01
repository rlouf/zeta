# Semantic Operators Roadmap

This is the planned breaking redesign for Sigil's shell glyphs. Do not preserve
the old glyph meanings during this migration.

## Target Grammar

```text
?    answer from read
??   answer from read + web

,    propose
,,   one agent step, confirm effects
,,,  one agent step, auto-approve routine effects

@    agent goal loop, confirm each step/checkpoint
@@   agent goal loop, auto-approve routine steps
```

The design separates the axes:

- `?` controls information sources.
- `,` delegates one next action.
- `@` pursues a durable goal across multiple steps.

More punctuation grants more authority on the same axis. It should not secretly
change the unit of work.

## Removed Behavior

The migration intentionally removes these meanings:

- `??` no longer means follow up on the previous answer.
- `???` no longer means exhaustive question.
- `,,` no longer means generate and execute one shell command.
- `,,,` no longer means the only agentic/editing route.

If follow-up remains useful, keep it as explicit CLI behavior such as
`sigil ask --follow-up`, not as a glyph. If exhaustive answers remain useful,
make them prompt text or a long-form flag, not `???`.

## Question Routes

`?` answers with local read-only context. It may use shell/session context and
the read-only inspection tools (`read`, `grep`, `find`, `ls`), but it must not
authorize web search.

`??` answers with local read-only context plus web search. This is the explicit
web authorization route.

Both routes have no Bash execution path. If an answer recommends a command, it
is plain answer text.

Implementation notes:

- Refactor `ask()` to accept explicit `glyph`, `tools`, and `use_web` or source
  authorization parameters.
- Route `?` to `--tools read,grep,find,ls`.
- Route `??` to `--tools read,grep,find,ls,web_search`.
- Record `?` and `??` to the event log under their route glyph.
- Reject `???`.

## Comma Routes

`,` proposes the next command/action and changes nothing.

`,,` runs one agent step after showing the step and asking before effects.

`,,,` runs one agent step without routine per-step confirmation. The shell
remains the review boundary: Bash tool execution is blocked and staged as a
command for explicit review rather than run inline.

Implementation notes:

- Keep the current `,` proposal surface where possible.
- Refactor the existing act stepper so it can run with `confirm_step=True` for
  `,,` and `confirm_step=False` for `,,,`.
- Make the step runner accept the originating glyph so tool traces match the
  route.
- Preserve the "one step, then return control" invariant for both `,,` and
  `,,,`.

## Goal Routes

`@` starts or resumes a durable goal loop with confirmation at each step or
checkpoint.

`@@` starts or resumes a durable goal loop that auto-approves routine steps.

The goal loop is similar in spirit to Codex `/goal`: it pursues an objective
until completion, blockage, budget exhaustion, or interruption. It is not an
unbounded shell loop.

Recommended goal statuses:

```text
active
completed
blocked
budget_hit
aborted
```

Recommended state file:

```text
last-goal.jsonl
```

Recommended goal shape:

```json
{
  "goal_id": "...",
  "objective": "...",
  "status": "active",
  "approval": "confirm",
  "steps": [],
  "budgets": {
    "max_steps": 5
  }
}
```

Goal loops should have default budgets. `@@` must stop on unclear status or
budget exhaustion rather than continuing indefinitely.

## Parser Rules

Supported operators:

```text
?   depth 1..2
,   depth 1..3
@   depth 1..2
```

Invalid:

```text
???
@@@
mixed glyph tokens such as ,?
```

## TODO

- [x] Update operator parsing to support `@` and per-glyph max depths.
- [x] Reject `???` and `@@@` with clear errors.
- [x] Refactor `ask()` to use explicit source authorization instead of
      `follow_up`.
- [x] Route `?` through read-only tools without web search.
- [x] Route `??` through read plus web search.
- [x] Update question trust fields so only `??` carries the `network` label.
- [x] Remove glyph-level follow-up and exhaustive-question behavior.
- [x] Keep or remove `sigil ask --follow-up` as an explicit CLI decision.
- [x] Refactor the act stepper to accept `confirm_step` and `glyph`.
- [x] Route `,,` to one confirmed agent step.
- [x] Route `,,,` to one auto-approved agent step within policy.
- [x] Ensure `,,,` still stops at explicit policy boundaries.
- [x] Extract shared Zeta agent-step execution for comma and goal routes.
- [x] Add `goals.py` or equivalent durable goal-loop module.
- [x] Add goal state recording in `last-goal.jsonl`.
- [x] Implement `@` as a confirmed goal loop with checkpoints.
- [x] Implement `@@` as an auto-approved goal loop with budgets and policy
      stops.
- [x] Add structured goal step status detection: continue, complete, blocked.
- [x] Update zsh bindings for `?`, `??`, `,`, `,,`, `,,,`, `@`, and `@@`.
- [x] Update Bash bindings for `?`, `??`, `,`, `,,`, `,,,`, `@`, and `@@`.
- [x] Remove shell bindings for `???`.
- [x] Update README glyph reference and examples.
- [x] Update CLI docs and trust model docs.
- [x] Rewrite tests for question routing and alpha trust records.
- [x] Rewrite tests for comma routing.
- [x] Add parser and shell binding tests for `@` and `@@`.
- [x] Add tests that `???` and `@@@` fail.
- [x] Run the full test suite.
