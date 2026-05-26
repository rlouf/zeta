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
,   recommend a concrete next action
,,  generate and execute a shell command
?   answer a question with Pi using read + web search
??  continue the previous question discussion
^   preview a repair for the last failure or stdin targets
^^  run a deeper repair preview pass
```

It maps to the lattice as follows:

```text
,   human prompt -> model recommendation
    integrity=local_model
    capability=propose
    taint=["model"]
    provisional=false

,,  human prompt -> generated command execution
    integrity=local_model
    capability=exec_boxed
    taint=["model"]

^   failed command/files -> repair preview
    integrity=local_model
    capability=propose
    taint=["model"]

^^  failed command/files -> deeper repair preview
    integrity=local_model
    capability=propose
    taint=["model"]

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

The selector prefix is part of the interaction contract. A command written to
shell history should reveal whether it was model-authored, web-tainted, or
legacy before the user recalls it for review.

## Continuation Rules

Continuations inherit maximum taint and minimum integrity from their inputs.

For the legacy `sigil command --previous` selector, `last-command.json` stores
the event ID of the previous command generation. Reopening the selector creates
a `command_continued` event with that input ID, and the final
`command_selected` event points to the continuation.

For `??`, `last-question.jsonl` stores the user and assistant transcript turns
with their originating event IDs. A follow-up consumes those transcript records,
inherits their taint and integrity, and records the consumed IDs in the new
question event.

This means the event log can reconstruct every continuation input instead of
relying on shell globals or implicit session memory.

Use `sigil events lineage [event-id]` to inspect the recorded provenance chain.
Without an event id, Sigil shows the latest event from the current shell session.

Session state files can be inspected with `sigil session show` for debugging.
This does not call a model, append events, or create executable shell text.

Failure repair records may include bounded stdout/stderr snippets and safe local
cwd/git context. These are inputs to model-authored repair proposals, so `^` and
`^^` remain `local_model / propose / model-tainted` and only print repair
previews.

When repair output is a unified diff, Sigil stores it as a patch preview. The
preview can be checked with `sigil patch check`; applying it requires
`sigil patch apply --yes` and records a `write_boxed` event linked to the patch
preview.

## Enforcement Before New Glyphs

The lattice exists before higher-risk glyphs are added. Current enforcement is
fail-closed:

```text
no ?! parser route
no promotion mutation
```

The zsh and Bash bindings do not map `?!`, `,!`, `@`, or `@!` to parser routes.
Current grammar must preserve these constraints:

- `??` after `?` must never produce an executable proposal.
- `,,` must record the generated-command event and the boxed execution event.
- Legacy state must remain visibly low-trust.
- Event logs must retain enough input IDs to reconstruct continuations.
- Tests must fail for any path that increases integrity without fresh human
  input.

## User Mental Model

The short version:

- `,` means "ask the local model to recommend one concrete next action."
- `,,` means "ask the local model for one shell command and execute it."
- `?` means "answer using read and web search; do not execute."
- `??` means "continue that read/web discussion; still do not execute."

Read/web routes produce answers, not commands. Execution routes must be boxed,
explicit, and lower or preserve integrity unless the user provides fresh input.
