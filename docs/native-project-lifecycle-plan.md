# Native Project Lifecycle Plan

## Goal

Run one explicit project revision in one native Zeta process.

## Command contract

`zeta check` reads and validates the current project source. It writes nothing.

`zeta reload` reads the current project source. It writes one verified active
revision. A live runtime adopts that revision without a process restart.

`zeta up` starts the runtime with the active revision. It does not read
draft agent source.

`zeta down` stops the runtime. It preserves the active revision and runtime
state.

`zeta status` reports runtime health, the active revision, and active agents.

## Source contract

The first release reads direct Markdown files from `agents/`. Each file name
defines one agent slug. The loader sorts files before it builds a revision.

The active-revision document stores the exact parsed agent source and its
content addresses. It lives in the selected runtime state directory.

The loader rejects a missing `agents/` directory, malformed Markdown, invalid
slugs, duplicate slugs, and symbolic-link agent entries.

## Reload contract

`reload` creates a new revision only after every agent source validates.
The command atomically replaces the active-revision document.

When the runtime is live, the application socket owns the replacement. New
queries observe the new revision after that replacement completes.

An edited agent retains its prior active version until reload succeeds. A new
agent remains absent until reload succeeds. A failed reload preserves the
prior active revision.

## Scope boundary

This slice loads source, creates revisions, lists agents, and reloads them. It does not yet claim
work, execute model calls, run schedules, or activate connectors.

The next slice will compile the complete execution manifest. It will add model,
capability, connector, and write-scope validation before work execution.
