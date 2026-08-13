# Native Project Lifecycle Plan

## Goal

Run one explicit project generation in one native Zeta process.

## Command contract

`zeta check` reads and validates the current project source. It writes nothing.

`zeta reload` reads the current project source. It writes one verified active
generation. A live runtime adopts that generation without a process restart.

`zeta up` starts the runtime with the active generation. It does not read
draft agent source.

`zeta down` stops the runtime. It preserves the active generation and runtime
state.

`zeta status` reports runtime health, the active generation, and active agents.

## Source contract

The first release reads direct Markdown files from `agents/`. Each file name
defines one agent slug. The loader sorts files before it builds a generation.

The active-generation document stores the exact parsed agent source and its
content addresses. It lives in the selected runtime state directory.

The loader rejects a missing `agents/` directory, malformed Markdown, invalid
slugs, duplicate slugs, and symbolic-link agent entries.

## Reload contract

`reload` creates a new generation only after every agent source validates.
The command atomically replaces the active-generation document.

When the runtime is live, the application socket owns the replacement. New
queries observe the new generation after that replacement completes.

An edited agent retains its prior active version until reload succeeds. A new
agent remains absent until reload succeeds. A failed reload preserves the
prior active generation.

## Scope boundary

This slice loads, snapshots, lists, and reloads agents. It does not yet claim
work, execute model calls, run schedules, or activate connectors.

The next slice will compile the complete execution manifest. It will add model,
capability, connector, and write-scope validation before work execution.
