# Shell Workflow

Sigil keeps the shell as the main interface. Use the long-form CLI verbs when
you want explicit commands, and use glyphs when you want fast interactive
turns.

## Common Workflows

Generate commands:

```sh
sigil command "find large files"
sigil command "show modified Python files"
```

Ask questions:

```sh
sigil ask "what changed in this repo?"
sigil ask --follow-up "what should I test?"
, why did that command fail?
```

Work from stdin:

```sh
git diff | sigil ask "review risky changes"
git diff --name-only | sigil command "choose a focused test command"
git diff | , explain the riskiest part
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

## Review Points

The shell remains the review boundary:

- `,` answers from read-only context.
- `,,` runs one Zeta agent step after confirming effects.
- `,,,` runs one Zeta agent step without routine confirmation.

## Session Continuity

Installed shell bindings set `SIGIL_SESSION_ID` once when the shell starts.
That keeps answer transcripts, failure context, and act state scoped to one
terminal window by default.

Useful inspection commands:

```sh
sigil session show
sigil session path
sigil events
```

Use `sigil session clear` to remove the current session's continuity files.
