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
?   local inspect question
??  web-authorized question discussion
^   recommend a repair for the last failure or stdin targets
^^  preview and confirm generated repair application
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

^   failed command/files -> repair proposal
    integrity=local_model
    capability=propose
    taint=["model"]

^^  failed command/files -> confirmed repair application
    integrity=local_model
    capability=propose, then write_boxed or exec_boxed after confirmation
    taint=["model"]

?   local inspect question
    integrity=local_model
    capability=read
    taint=["model"]
    provisional=false

??  question continuation
    inherits previous question transcript integrity and taint
    capability=read
    taint includes "web"
    provisional=true
```

The `?` route uses the local inspect operator. The `??` route invokes Pi with
`read,web_search`, so it is web-tainted by construction. Both routes are
read-only and have no execute path.

## Visible Descent

Sigil makes trust visible in the terminal.

Headers:

```text
❯ sigil ,  · propose · model-authored
❯ sigil ,, · inherited: model
❯ sigil ?  · read · model-authored
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

For `??`, `last-question.jsonl` stores the user and assistant transcript turns
with their originating event IDs. A follow-up consumes those transcript records,
inherits their taint and integrity, and records the consumed IDs in the new
question event.

This means the event log can reconstruct every continuation input instead of
relying on shell globals or implicit session memory.

Use `sigil events lineage [event-id]` to inspect the recorded provenance chain.
Without an event id, Sigil shows the latest event from the current shell session.

Session state files can be inspected with `sigil session show` for debugging.
This does not call a model, append events, or create executable shell text. A
session is one terminal shell by default; `SIGIL_SESSION_ID` or
`SIGIL_SESSION_DIR` can intentionally override that boundary.

Failure repair records may include bounded stdout/stderr snippets and safe local
cwd/git context. These are inputs to model-authored repair proposals, so `^`
remains `local_model / propose / model-tainted`. `^^` starts with the same
model-authored proposal, then requires confirmation before crossing into
`write_boxed` for patch application or `exec_boxed` for a repair command.

When double repair output is a unified diff, Sigil stores it as a patch preview
before confirmation. The preview can also be checked later with `sigil patch
check`; applying it later requires `sigil patch apply --yes` and records a
`write_boxed` event linked to the patch preview.

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
