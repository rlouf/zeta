# Semantic operators roadmap

This document describes how Sigil can move from its current stable state to the
semantic-operator model proposed for the README:

```text
?   inspect
,   propose
^   repair
:   transform
```

The goal is to move from stable state to stable state. The existing prompt
integration, command proposal flow, repair flow, and trust lattice should remain
working while stream semantics are added underneath them.

## Current stable state

Sigil is currently a punctuation-native shell assistant with a Python CLI core
and thin Bash/zsh bindings.

Implemented grammar:

```text
,   generate shell command candidates
,,  reopen the previous command selector
?   answer a question with Pi using read + web search
??  continue the previous question discussion
^   suggest fixes for the last failed command
^^  reopen previous fix candidates
@.  summarize the current Sigil session without mutation
```

Important existing foundations:

- `src/sigil/cli.py` is the shell-agnostic CLI boundary.
- `src/sigil/commands.py` owns command proposal.
- `src/sigil/question.py` owns the current Pi-backed question path.
- `src/sigil/failure.py` owns last-failed-command repair.
- `src/sigil/security.py` owns the trust lattice.
- `src/sigil/state.py` and `src/sigil/session.py` provide durable event and
  session state.
- `shell/zsh/sigil.zsh` and `shell/bash/sigil.bash` provide prompt integration.

The strongest asset is the security lattice. It already records integrity,
capability, taint, provisional status, and provenance links. The semantic
operator runtime should use this lattice rather than bypassing it.

## Target stable state

Sigil should behave like a small set of cognitive Unix operators:

```sh
git diff | ?? review risky changes
find . -name "*.md" | ?? summarize decisions
rg -l "FooClient" src | ^^ rename to Client
cat notes.txt | , draft an executive summary
```

The shell remains the orchestration layer. Operators should consume stdin, emit
stdout, and write trust/provenance metadata to Sigil state.

The semantic center should become:

```text
stdin + operator + depth + prompt + cwd + trust metadata -> stdout + event log
```

## Migration strategy

### 1. Preserve current behavior

Do not begin by redefining the existing glyphs in the shell bindings.

The current interactive behavior is useful and tested:

- `,` and `^` insert selected commands back into the prompt for human review.
- `?` and `??` produce read-only answers.
- `@.` is read-only session inspection.

These should remain the compatibility surface while the new runtime is added.

### 2. Add an operator runtime

Add a new module, likely `src/sigil/operators.py`, and a hidden CLI route such
as:

```sh
sigil op "??" --prompt "review risky changes"
```

Milestone 1 should only cover glyph families that already exist in Sigil:
`?`, `,`, and `^`. The `:` transform operator remains a target semantic, but
should not be accepted by the parser until the transform workflow is designed.

The runtime should parse repeated glyphs into:

```text
base operator: ? | , | ^ | :
depth:         1 | 2 | 3 | ...
prompt:        user text after the glyph
stdin:         captured input stream
mode:          interactive | pipeline
```

This is the right place to centralize semantics. Shell files should not each
learn separate meanings for `?`, `??`, and `???`.

### 3. Make shell bindings pipeline-aware

Update Bash and zsh functions so they branch on whether stdin is a terminal.

When stdin is a terminal, keep the current prompt-buffer behavior.

When stdin is piped, call the operator runtime:

```sh
git diff | ?? review risky changes
# shell binding calls something like:
# sigil op "??" --prompt "review risky changes"
```

This creates the first new stable state: existing interactive workflows still
work, and piped semantic operators become available.

### 4. Define operator contracts

Each operator should have an explicit IO contract.

```text
? inspect
  Input:  stdin, prompt, optional local context
  Output: explanation, analysis, summary, review
  Writes: event log and optional transcript
  Default capability: read

, propose
  Input:  stdin, prompt, cwd
  Output: proposed command, text, plan, or draft
  Writes: event log and last proposal state
  Default capability: propose

^ repair
  Input:  failed command state or stdin targets, prompt
  Output: repair suggestion, command, or patch preview
  Writes: event log and last repair state
  Default capability: propose; write only through explicit boxed paths

: transform
  Input:  stdin and transform prompt
  Output: transformed stdout
  Writes: event log
  Default capability: read or write_boxed depending on destination
```

Stdout should carry the composable payload. Status lines, trust labels,
previews, and rationale should go to stderr unless the operator is explicitly
asked for structured output.

### 5. Make repetition a policy input

Repetition should not be implemented as separate ad hoc commands.

Recommended first semantics:

```text
depth 1: quick, low-context, no mutation
depth 2: deeper, can inspect more local context, still preview-first
depth 3: autonomous loop within explicit safety policy
```

Examples:

```text
?      quick explanation
??     deeper investigation
???    exhaustive analysis

,      propose commands or text
,,     recommend best approach
,,,    infer and execute only through explicit execution policy

^      suggest repair
^^     apply likely repair only through preview/patch workflow
^^^    iterate until success only inside a boxed execution policy
```

The trust lattice should cap what each depth can do. More punctuation may mean
more effort or autonomy, but it must not silently grant destructive authority.

### 6. Separate old question behavior from inspect semantics

Today `?` means "ask Pi with read + web search." In the target model, `?` means
"inspect or analyze."

The stable migration is:

1. Keep interactive `?` and `??` using the current question route.
2. Implement piped `?`, `??`, and `???` through the shared operator runtime.
3. Move the interactive question route onto the same operator runtime once the
   stream path is stable.

This avoids breaking existing usage while moving toward the README semantics.

### 7. Extend repair in two modes

Current `^` repairs the last failed command. Keep that behavior when there is no
piped stdin.

Add a second repair mode for piped targets:

```sh
rg -l "FooClient" src | ^^ rename to Client
```

This mode should treat stdin as a target stream. It should first produce a
visible patch or command preview. File writes should go through the existing
capability lattice and should be tested separately from last-command repair.

### 8. Add transform as a first-class operator

The `:` operator should be a pure stream transform first.

Examples:

```sh
cat logs.txt | :json
cat notes.md | : bullets
cat report.md | : executive summary
```

Start with stdout-only transforms. Add file-writing transforms later through
the same boxed write policy used by repair.

## Safety requirements

Destructive operations must never execute silently.

Policy should classify at least:

- file modification
- network access
- privileged commands
- deletion
- shell execution

Commands, patches, and transformations that write outside stdout should be
visible before execution unless the user has explicitly selected a higher
autonomy route and the route is allowed by policy.

Existing protections to preserve:

- no `?!` execution path
- no auto-run from web-tainted state
- no promotion mutation
- no bang execution without a sandbox boundary
- legacy state remains visibly low-trust

## Suggested stable milestones

### Milestone 1: operator CLI

Status: implemented for `?`, `,`, and `^`.

Add `sigil op` and tests for parsing existing glyph families:

```text
? -> base=?, depth=1
?? -> base=?, depth=2
^^^ -> base=^, depth=3
```

The `:` transform operator is intentionally not accepted yet. Existing shell
behavior remains unchanged.

### Milestone 2: piped inspect and propose

Status: implemented for Bash and zsh wrapper dispatch.

Support:

```sh
git diff | ?? review risky changes
cat notes.txt | , draft an executive summary
```

No mutation. No command execution. Record events with trust metadata.

### Milestone 3: piped repair previews

Support:

```sh
rg -l TODO src | ^^ generate cleanup patch
```

The first stable version should preview a patch or command sequence. Applying
patches can come after the preview path is well tested.

### Milestone 4: transform operator

Support:

```sh
cat logs.txt | :json
cat report.md | : summarize
```

Keep output on stdout so normal Unix composition works.

### Milestone 5: unify interactive glyphs on the operator runtime

Once stream semantics are stable, make interactive `?`, `,`, and `^` call the
same operator core where practical.

The prompt-buffer UX can remain. The implementation should converge.

### Milestone 6: gated autonomy

Only after the above is stable, implement higher-autonomy meanings such as:

```text
,,, infer and execute
^^^ iterate until success
```

These require explicit execution policy, patch previews, provenance, and tests.

## `@.` status

`@.` is already implemented.

It routes to `sigil summary`, which reads current session state and prints a
summary without mutating state. Both shell bindings support it:

- Bash defines `function @. { sigil_summary "$*"; }`
- zsh defines `function '@.' { sigil_summary "$*" }`

Prompt-buffer dispatch is also implemented for `@. ...` in both shells. Tests
cover the route as read-only behavior.

What is not implemented yet:

- `@@` search over past Sigil memory
- model-generated session summaries
- promotion or mutation routes through `@`

The current `@.` is deliberately local, read-only session inspection.
