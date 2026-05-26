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
,   recommend a concrete next action
,,  generate and execute a shell command
?   local inspect question
??  web-authorized question discussion
^   recommend a repair for the last failed command or targets
^^  preview and confirm generated repair application
```

Important existing foundations:

- `src/sigil/cli.py` is the shell-agnostic CLI boundary.
- `src/sigil/commands.py` owns command proposal.
- `src/sigil/question.py` owns the current Pi-backed question path.
- `src/sigil/failure.py` owns last-failed-command repair.
- `src/sigil/security.py` owns the trust lattice.
- `src/sigil/state.py` and `src/sigil/session.py` provide durable event and
  session state.
- `shell/zsh/sigil.zsh` and `shell/bash/sigil.bash` provide shell integration.

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

### 1. Preserve command verbs while moving glyphs

Keep the long-form `sigil command`, `sigil ask`, and `sigil fix` verbs available
while glyphs move to the operator runtime.

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

Update Bash and zsh functions to call the operator runtime:

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

Current semantics:

```text
comma depth 1: recommend one concrete next action
comma depth 2: generate and execute one shell command
other depth 1: quick, low-context, no mutation
repair depth 2: preview a generated repair, then apply only after confirmation
```

Piped input is treated as opaque context. Comma and repair routes preview piped
input and require confirmation before using it; piped comma depth 2 also
requires command confirmation before execution.

Examples:

```text
?      quick explanation
??     deeper investigation
???    exhaustive analysis

,      recommend best approach
,,     infer and execute

^      recommend repair
^^     preview and confirm generated repair application
^^^    iterate until success only inside a boxed execution policy
```

The trust lattice should cap what each depth can do. More punctuation may mean
more effort or autonomy, but it must not silently grant destructive authority.

### 6. Separate old question behavior from inspect semantics

Today `?` means "inspect or analyze." The web-authorized question route lives on
`??`.

The stable migration is:

1. Keep interactive `?` on the shared operator runtime.
2. Keep `??` as the explicit web-authorized question route.
3. Implement any future deeper inspect forms through the shared operator runtime.

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
capability lattice and should be tested separately from failure repair.

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
- no promotion mutation
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

Status: implemented for Bash and zsh wrapper dispatch, plus piped `?` / `??`
inspect, `,` recommendation output, and `,,` command execution.

Support:

```sh
git diff | ?? review risky changes
cat notes.txt | , draft an executive summary
```

Single comma has no mutation. Double comma executes and records events with
trust metadata.

### Milestone 3: piped repair previews

Status: implemented as model-generated preview output for piped `^` / `^^`.

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

Status: implemented. Shell glyph wrappers and prompt dispatch now use `sigil op`
directly; the public verb CLI routes remain available.

Interactive `?`, comma, and repair glyphs use the operator core. `??` remains
the explicit web-authorized question route.

### Milestone 6: comma autonomy

```text
,    recommend a concrete next action
,,   execute
```

Double comma executes the generated shell command directly. `--dry-run` remains
available for inspection, and command execution is recorded with provenance.

### Milestone 6: action classification

Status: implemented. Operator output is classified into action classes for
audit/debugging, and `--dry-run` is available at the CLI boundary. Comma depth
semantics are explicit: `,` recommends and `,,` executes.

### Milestone 7: patch application workflow

Status: implemented. Double repair operators that emit unified diffs store a
patch preview in session state, show it, and ask before applying it. `sigil
patch show` prints the latest preview, `sigil patch check` validates it with
`git apply --check`, and `sigil patch apply --yes` applies it explicitly with
`git apply` while recording provenance events.

## `@@` status

`@@` search over past Sigil memory is not yet implemented. Session state can be
inspected with `sigil session show`.
