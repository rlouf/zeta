# Shell Workflow

Sigil keeps the shell as the main interface. Use the long-form CLI verbs when
you want explicit commands, and use glyphs when you want fast interactive
turns.

## Common Workflows

Generate commands:

```sh
sigil command "find large files"
sigil command "show modified Python files"
, run the relevant tests
```

Ask questions:

```sh
sigil ask "what changed in this repo?"
sigil ask --follow-up "what should I test?"
? why did that command fail?
?? what changed in the latest release notes?
```

Work from stdin:

```sh
git diff | sigil ask "review risky changes"
git diff --name-only | sigil command "choose a focused test command"
git diff | ? explain the riskiest part
```

Run one agent step:

```sh
,, run the formatter for files I changed
```

Let Zeta take one routine bounded step:

```sh
,,, fix the failing parser test
sigil act show
```

Pursue a bounded goal:

```sh
@ fix the failing parser test
@@ update docs and run checks
```

## Review Points

The shell remains the review boundary:

- `,` proposes and does not execute.
- `,,` runs one Zeta agent step after confirming effects.
- `,,,` runs one Zeta agent step without routine confirmation.
- `@` runs a bounded goal loop with checkpoints.
- `@@` runs a bounded goal loop with routine auto-approval.
- `?` answers from local read-only context.
- `??` answers from local context plus web search.

## Session Continuity

Installed shell bindings set `SIGIL_SESSION_ID` once when the shell starts.
That keeps question transcripts, failure context, and act state scoped to one
terminal window by default.

Useful inspection commands:

```sh
sigil session show
sigil session path
sigil events
```

Use `sigil session clear` to remove the current session's continuity files.
