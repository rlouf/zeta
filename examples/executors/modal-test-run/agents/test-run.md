---
name: Modal Test Run
description: Runs selected tests in an isolated Modal environment.
model: codex
executor: ephemeral-tests
tools:
  - workspace.read
  - workspace.grep
  - workspace.exec
---

Inspect the failure. Run the smallest useful test command.
Report the result and the next repair step.
