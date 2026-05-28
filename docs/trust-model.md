# Sigil Trust Model

Alpha Sigil keeps the trust model small:

- small blast radius
- inspectable history
- explicit confirmation before generated commands can become actions

The user-facing boundary is a route `mode`, plus a short list of risk labels
when they matter.

## Modes

```text
read-only      answer from available context; no Bash execution path
propose        suggest a command or next action; do not run it
execute-write  one bounded Pi step may read, edit, write, or hand off Bash
```

## Risk Labels

Events and command proposals may include these labels:

```text
network
delete
publish
privileged
```

No label means Sigil did not detect one of those alpha risks. It is not a
guarantee that the command is harmless.

## Event Records

Event records answer the practical audit questions:

- what was suggested
- why it was suggested, when a model supplied that reason
- what ran or was handed off
- what changed or failed, when Sigil can observe it

The alpha trust fields are:

```json
{
  "glyph": ",",
  "mode": "propose",
  "labels": ["network"],
  "inputs": ["event-id"]
}
```

`inputs` is a simple list of earlier event ids used as context. It is kept only
so `sigil events lineage` can show a short audit trail.

## Route Rules

```text
?    read-only answer from local context
??   read-only answer with web/network authorization
,    command or action proposal
,,   one execute-write Pi step after confirmation
,,,  one execute-write Pi step with routine confirmation skipped
@    bounded execute-write goal loop with checkpoints
@@   bounded execute-write goal loop with routine confirmation skipped
```

Question routes never expose Bash. If an answer recommends a command, it is
plain answer text.

Comma and goal routes may hand off a Bash command, but Pi does not run it
directly. The shell binding puts the command back under user review.

## Inspection

```sh
sigil events
sigil events lineage
sigil events lineage EVENT_ID --json
```

Example event table:

```text
time      id        action       trust          session   summary
12:00:01  e3b0c442  ? question   read-only      9aa2f6e1  what changed?
12:01:10  2f7d6a8c  , recommend  propose        9aa2f6e1  run the tests
12:01:18  b1c4a901  ,, step      execute-write  9aa2f6e1  run the tests
```

## Post-Alpha

Deferred until it changes confirmation, display, or audit behavior:

- a formal trust vocabulary beyond the three modes
- additional event fields that are not shown to users
- deeper audit graphs than the short `inputs` trail

Add more machinery only when it changes confirmation, display, or audit
behavior.
