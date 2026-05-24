# Security Lattice

Sigil treats shell interaction as a trust boundary. A glyph can ask a model for a
proposal, continue a prior interaction, read local or web material, or eventually
request boxed writes/execution. The security lattice is the ABI that keeps those
steps explicit.

The implementation lives in `src/sigil/security.py`. Event writers in
`src/sigil/state.py` normalize the fields for global events and per-session JSONL
state.

## Primitives

Every event and session record can carry:

```json
{
  "glyph": "?",
  "inputs": ["event-id"],
  "integrity": "web",
  "capability": "read",
  "taint": ["web", "model"],
  "provisional": true
}
```

`glyph` is the grammar token that produced the record.

`inputs` is the list of event IDs consumed to produce this record. Continuations
must include the previous event IDs they inherit from, so the event log can
reconstruct why a later answer or proposal has its trust level.

`integrity` is the origin-quality label:

```text
human > local_model > local_file > web > unknown
```

Integrity only descends across continuations. A continuation can preserve or
lower integrity, but it cannot raise it. Only fresh human input can create a new
higher-integrity boundary.

`capability` is the maximum action class the invocation is allowed to represent:

```text
none < propose < read < write_boxed < exec_boxed
```

Capability is capped by the invocation route. Today the implemented routes are
only `propose` and `read`.

`taint` is the accumulated source set. It is intentionally simple and visible:
`model`, `web`, and `legacy` are the important current labels. Continuations
inherit the union of previous taint labels.

`provisional` marks output that should not be treated as stable authority. The
current web-backed question path is provisional by construction.

## Legacy State

Old Sigil state did not have trust fields. When a JSON or JSONL record is read
without `integrity` and `taint`, Sigil normalizes it as:

```text
integrity = unknown
capability = none
taint = ["legacy"]
provisional = false
inputs = []
```

This is deliberately conservative. Legacy command suggestions can still be
reopened, but they display as low-trust and continuations inherit that low-trust
state.

## Current Grammar Mapping

The implemented grammar is:

```text
,   generate shell command candidates
,,  reopen the previous command selector
?   answer a question with Pi using read + web search
??  continue the previous question discussion
```

It maps to the lattice as follows:

```text
,   human prompt -> model proposal
    integrity=local_model
    capability=propose
    taint=["model"]
    provisional=false

,,  previous command continuation
    inherits previous command integrity and taint
    capability=propose

?   read + web question
    integrity=web
    capability=read
    taint=["web"]
    provisional=true

??  question continuation
    inherits previous question transcript integrity and taint
    capability=read
    taint includes "web"
    provisional=true
```

The `?` and `??` routes invoke Pi with `read,web_search`. They are therefore
web-tainted by construction. They are read-only routes and have no execute path.

## Visible Descent

Sigil makes trust visible in the terminal.

Headers:

```text
❯ sigil ,  · propose · model-authored
❯ sigil ,, · inherited: model
❯ pi ?     · read+web · no execute path
❯ pi ??    · inherited: web · provisional
```

Command selectors also prefix candidates:

```text
[model/propose] git status --short
[web-tainted/provisional] ...
[legacy/low-trust] ...
```

The selector prefix is part of the interaction contract. A command inserted into
the shell prompt should reveal whether it was model-authored, web-tainted, or
legacy.

## Continuation Rules

Continuations inherit maximum taint and minimum integrity from their inputs.

For `,,`, `last-command.json` stores the event ID of the previous command
generation. Reopening the selector creates a `command_continued` event with that
input ID, and the final `command_selected` event points to the continuation.

For `??`, `last-question.jsonl` stores the user and assistant transcript turns
with their originating event IDs. A follow-up consumes those transcript records,
inherits their taint and integrity, and records the consumed IDs in the new
question event.

This means the event log can reconstruct every continuation input instead of
relying on shell globals or implicit session memory.

## Enforcement Before New Glyphs

The lattice exists before higher-risk glyphs are added. Current enforcement is
fail-closed:

```text
no ?! parser route
no auto-run from web-tainted state
no promotion mutation
no bang unless sandbox exists
```

The zsh binding blocks `?!`, `,!`, `@`, and `@!` before they can become parser
routes. The Python security helpers also expose checks for future routes:

```text
reject_promotion(...)
ensure_no_auto_run(...)
require_sandbox_for_bang(...)
```

Future grammar must use these helpers or stricter equivalents. In particular:

- `??` after `?` must never produce executable insertion.
- `,,` must inherit taint from the prior command event.
- Legacy state must remain visibly low-trust.
- Event logs must retain enough input IDs to reconstruct continuations.
- Tests must fail for any path that increases integrity without fresh human
  input.

## User Mental Model

The short version:

- `,` means "ask the local model to propose shell text."
- `,,` means "show me that same proposal context again, with the same trust."
- `?` means "answer using read and web search; do not execute."
- `??` means "continue that read/web discussion; still do not execute."

Sigil inserts text into the prompt only where the route is a proposal route.
Read/web routes produce answers, not commands. Any future write or execution
route must be boxed, explicit, and lower or preserve integrity unless the user
provides fresh input.
