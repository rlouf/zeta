# Zeta Refactor Plan

## What We Want

Opening `src/zeta/` should make the system obvious:

```text
records       what happened and what it means
run           how one run happens
context       what the model sees for a run
models        how providers become normalized model input/output
capabilities  how tools are declared, resolved, authorized, staged, invoked
orchestration how accepted events become work
rpc           how external clients talk to Zeta
cli.py        how humans inspect and drive Zeta from a shell
process.py    how Zeta is assembled for this machine/process
```

The code should read as a set of clear lifecycles:

- Records remember durable facts and reconstruct meaning from them.
- Context builds the model input for a run.
- Runtime executes one run inside a thread.
- Models normalize provider APIs.
- Capabilities normalize tool declarations and execution.
- Orchestration turns accepted events into work.
- RPC and CLI expose operations without owning behavior.
- Process wires concrete local services without owning behavior.

The most important boundaries:

- Records do not execute.
- Context does not execute.
- Runtime does not persist directly or know whether it was called by CLI, RPC,
  worker, or test.
- Models do not decide when to call a model.
- Capabilities do not decide when a tool call should be handled.
- Orchestration does not know the model/tool loop.
- RPC and CLI do not own runtime, dispatch, model, or store behavior.
- Process wires components together; it does not define their behavior.

## Refactoring Style

This is not a compatibility-preserving file shuffle. Each phase should use the
move to simplify the code in that slice.

We are allowed to clean aggressively. The goal is not to preserve the current
shape under new filenames; the goal is to make the code read clearly after each
move. If the old structure created a dozen tiny functions, forwarding layers,
or names that no longer explain anything, inline them or delete them in the
same phase. It is better to start with a few direct, readable functions and
split them later than to carry a million small helpers whose only purpose was
to survive the refactor.

Treat the existing boundaries as evidence, not as a contract. When a move shows
that a layer was only preserving legacy shape, remove the layer. If a helper is
single-use and its name does not carry real domain meaning, inline it. The plan
should leave behind the simplest readable version of each slice, not a
temporarily renamed copy of the old system.

- Clean aggressively inside the active slice. If a name, helper, class, module,
  or layer exists only because the old structure made it necessary, remove it.
- Delete compatibility shims unless there is a real external import contract.
- Inline single-use helpers when their name does not clarify the story.
- Prefer fewer readable functions over many tiny forwarding helpers; splitting
  can happen later when a concept has earned its own name.
- Collapse dead abstractions exposed by the move.
- Remove old modules when emptied.
- Avoid new `manager`, `handler`, `utils`, or `common` buckets.
- Keep public seams only where they map to the agreed concepts: records, run,
  context, models, capabilities, orchestration, rpc, cli, and process.
- Let tests follow behavior, not the old internal structure.

Aggressive cleanup should stay inside the active slice. For example, a records
phase may aggressively clean records/events/stores/timeline code, but should not
opportunistically rewrite the run loop.

## Target Shape

```text
src/zeta/
  __init__.py
  cli.py
  process.py

  records/
    __init__.py
    events.py
    objects.py
    timeline.py
    provenance.py
    stores/
      __init__.py
      event_store.py
      object_store.py
      sqlite.py
      memory.py

  run/
    __init__.py
    runs.py
    threads.py
    runtime.py
    thread_run.py
    cancellation.py
    outcomes.py

  context/
    __init__.py
    builder.py
    prompts.py
    components.py
    instructions.py
    system.py
    budget.py
    transforms.py
    compaction/
      __init__.py
      drop_oldest.py
      structural_trim.py
      task_state.py

  models/
    __init__.py
    types.py
    profiles.py
    chat_completions.py
    responses.py
    codex_auth.py

  capabilities/
    __init__.py
    types.py
    registry.py
    execution.py

  orchestration/
    __init__.py
    agents.py
    dispatch.py
    worker.py
    scheduling.py
    queue.py
    attempts.py

  rpc/
    __init__.py
    jsonrpc.py
    stdio.py
    routes.py
```

The nouns live with the package that uses them:

```text
Event, DraftEvent       records/events.py
Object, Derivation      records/objects.py
EventStore              records/stores/event_store.py
ObjectStore             records/stores/object_store.py
Run, RunStatus          run/runs.py
Thread                  run/threads.py
ModelInput/Output       models/types.py
Capability              capabilities/types.py
AgentDefinition         orchestration/agents.py
QueueItem               orchestration/queue.py
Attempt                 orchestration/attempts.py
```

## Current Module Map

### Records

Current modules:

- `src/zeta/events.py`
- `src/zeta/history.py`
- `src/zeta/kernel/events.py`
- `src/zeta/kernel/objects.py`
- `src/zeta/store/events/filter.py`
- `src/zeta/store/events/memory.py`
- `src/zeta/store/events/sqlite.py`
- `src/zeta/store/events/__init__.py`
- `src/zeta/store/substrate/base.py`
- `src/zeta/store/substrate/memory.py`
- `src/zeta/store/substrate/sqlite.py`
- `src/zeta/store/substrate/__init__.py`
- `src/zeta/store/__init__.py`

Target destinations:

- `kernel/events.py` and durable-event helpers from `events.py` become
  `records/events.py`.
- `history.py` becomes `records/timeline.py`.
- `kernel/objects.py` becomes `records/objects.py`.
- Trace-object projection currently in `context/builder.py` becomes
  `records/provenance.py`.
- Store protocols currently in `store/events/__init__.py` and
  `store/substrate/base.py` become `records/stores/event_store.py` and
  `records/stores/object_store.py`.
- SQLite and in-memory store implementations move into
  `records/stores/sqlite.py` and `records/stores/memory.py`.

### Run

Current modules:

- `src/zeta/loop.py`
- `src/zeta/execute.py`
- `src/zeta/kernel/runs.py`
- `src/zeta/runtime/requests.py`
- `src/zeta/runtime/scope.py`
- `src/zeta/runtime/config.py`
- `src/zeta/agents/capabilities.py`

Target destinations:

- `kernel/runs.py` becomes `run/runs.py`.
- Thread scope concepts from `runtime/scope.py` become `run/threads.py`.
- Session/thread request parsing from `runtime/requests.py` becomes either
  `run/thread_run.py` if it is run-specific, or `rpc/routes.py` if it is only
  RPC input parsing.
- The model/tool loop from `loop.py` becomes `run/runtime.py`.
- Cancellation/deadline behavior from `loop.py` becomes `run/cancellation.py`.
- Run result and outcome shapes from `loop.py` become `run/outcomes.py`.
- Thread-run adaptation from `execute.py` becomes `run/thread_run.py` when it is
  a direct "run inside a thread" adapter.
- Agent run configuration from `agents/capabilities.py` should be split:
  run policy belongs in `run/runs.py` or `run/outcomes.py`; model status
  plumbing belongs near `models/types.py` or `run/runtime.py`.

### Context

Current modules:

- `src/zeta/context/__init__.py`
- `src/zeta/context/budget.py`
- `src/zeta/context/builder.py`
- `src/zeta/context/components.py`
- `src/zeta/context/instructions.py`
- `src/zeta/context/system.py`
- `src/zeta/context/transforms.py`
- `src/zeta/context/compaction/drop_oldest.py`
- `src/zeta/context/compaction/structural_trim.py`
- `src/zeta/context/compaction/task_state.py`
- `src/zeta/context/compaction/__init__.py`

Target destinations:

- Keep the package top-level as `context/`.
- Keep prompt components in `context/components.py`.
- Keep instruction/system loading in `context/instructions.py` and
  `context/system.py`.
- Keep compaction under `context/compaction/`.
- Split `context/builder.py`:
  - context planning and rendering stay in `context/builder.py` or
    `context/prompts.py`;
  - provenance projection moves to `records/provenance.py`.

### Models

Current modules:

- `src/zeta/models/__init__.py`
- `src/zeta/models/chat_completions.py`
- `src/zeta/models/codex_auth.py`
- `src/zeta/models/profiles.py`
- `src/zeta/models/responses.py`
- `src/zeta/kernel/models.py`

Target destinations:

- Keep the package top-level as `models/`.
- `kernel/models.py` becomes `models/types.py`.
- Provider adapters remain in `models/chat_completions.py` and
  `models/responses.py`.
- `models/codex_auth.py` stays with the Codex responses provider.
- `models/profiles.py` keeps model profile semantics, but process-local active
  profile state may move to `process.py` if it is only local assembly state.

### Capabilities

Current modules:

- `src/zeta/capabilities/base.py`
- `src/zeta/capabilities/registry.py`
- `src/zeta/capabilities/__init__.py`
- `src/zeta/kernel/capabilities.py`

Target destinations:

- Keep the package top-level as `capabilities/`.
- `kernel/capabilities.py` and capability declaration shapes become
  `capabilities/types.py`.
- `capabilities/registry.py` stays the registry/resolution boundary.
- Capability execution and staging behavior currently embedded in `loop.py` and
  `capabilities/base.py` should become `capabilities/execution.py`.

### Orchestration

Current modules:

- `src/zeta/dispatch.py`
- `src/zeta/agents/runtime.py`
- `src/zeta/agents/capabilities.py`
- `src/zeta/agents/__init__.py`
- `src/zeta/kernel/agents.py`
- `src/zeta/kernel/dispatch.py`
- `src/zeta/runtime/local.py`
- event-triggered pieces of `src/zeta/execute.py`

Target destinations:

- `kernel/agents.py` and compiled executable-agent concepts become
  `orchestration/agents.py`.
- `kernel/dispatch.py` queue/attempt shapes split into
  `orchestration/queue.py` and `orchestration/attempts.py`.
- Event routing and lifecycle behavior from `dispatch.py` becomes
  `orchestration/dispatch.py`.
- Worker claiming, leases, heartbeats, and queued execution from
  `runtime/local.py` become `orchestration/worker.py`.
- Schedule emission from `runtime/local.py` becomes
  `orchestration/scheduling.py`.
- If the `session.turn.requested` executable agent remains event-triggered, the
  relevant `execute.py` code becomes `orchestration/session_turn_agent.py`
  rather than `run/thread_run.py`.

### RPC

Current modules:

- `src/zeta/rpc/jsonrpc.py`
- `src/zeta/rpc/routes.py`
- `src/zeta/rpc/stdio.py`
- `src/zeta/rpc/__init__.py`

Target destinations:

- Keep `rpc/jsonrpc.py` and `rpc/stdio.py`.
- Keep route adapters in `rpc/routes.py` initially.
- Later split `routes.py` only when it improves navigation, for example:
  `rpc/session_routes.py`, `rpc/event_routes.py`, and `rpc/tool_routes.py`.

### CLI

Current module:

- `src/zeta/cli.py`

Target destination:

- Keep as top-level `zeta/cli.py`.
- It should parse flags, call `process.py`, and render output.
- It should not own worker, dispatch, store, model, or run behavior.

### Process

Current modules:

- `src/zeta/runtime/local.py`
- `src/zeta/runtime/config.py`
- `src/zeta/runtime/scope.py`
- local-state parts of `src/zeta/models/profiles.py`
- construction pieces of `src/zeta/execute.py`

Target destination:

- `zeta/process.py` is the high-level local entrypoint.
- It resolves state dirs/config files, opens stores, loads agent specs, selects
  models, opens threads, constructs workers, and provides concrete services to
  CLI/RPC/tests.
- It should not own the behavior of stores, workers, dispatch, models,
  capabilities, runtime, or RPC.

## Phased Plan

### Phase 0: Lock Current Behavior

Goal: make the refactor observable and reversible.

Tasks:

- Run the current test suite and record failures, if any.
- Identify the tests that cover:
  - `session.run` RPC behavior;
  - `events.publish` / `events.list`;
  - dispatch queue item and attempt lifecycle;
  - model request payloads and response parsing;
  - capability staging/direct execution;
  - trace/provenance projection;
  - Sigil workflows that call Zeta.
- Add focused tests only where a later move would otherwise be unpinned.

Do not move code in this phase.

### Phase 1: Create Records

Goal: move durable memory concepts without touching execution.

Tasks:

- Create `records/` and `records/stores/`.
- Move event domain shapes and event helper functions into `records/events.py`.
- Move object and derivation shapes into `records/objects.py`.
- Move event store and object store protocols into
  `records/stores/event_store.py` and `records/stores/object_store.py`.
- Move SQLite and memory implementations into `records/stores/sqlite.py` and
  `records/stores/memory.py`.
- Move `history.py` to `records/timeline.py`.
- Update imports in place.
- Remove old modules by the end of the phase; avoid long-lived compatibility
  shims.

Validation:

- Event store tests.
- History/timeline tests.
- RPC `events.list` tests.
- Dispatch lifecycle tests that read event history.

### Phase 2: Split Provenance From Context

Goal: keep context about model input, and records about replayable evidence.

Tasks:

- Move trace projection functions from `context/builder.py` into
  `records/provenance.py`.
- Keep prompt planning/rendering in `context/`.
- Introduce `context/prompts.py` only if it makes `context/builder.py` read
  better.
- Update runtime to record provenance through the new records boundary.

Validation:

- Prompt trace tests.
- Trace replay/render/query tests.
- Any tests asserting prompt component object ids.

### Phase 3: Promote Models And Capabilities

Goal: make the two extension seams explicit before splitting the run loop.

Tasks:

- Move `kernel/models.py` to `models/types.py`.
- Keep provider adapters under `models/`.
- Separate model profile semantics from local active-profile file state if that
  split is clear; otherwise leave `models/profiles.py` intact and flag the
  local state for the process phase.
- Move capability domain shapes to `capabilities/types.py`.
- Keep capability registry in `capabilities/registry.py`.
- Extract capability call handling/staging/execution from `loop.py` into
  `capabilities/execution.py`.

Validation:

- Model payload tests.
- Responses/Codex auth tests.
- Tool registration and schema validation tests.
- Staging/direct execution tests.

### Phase 4: Build The Run Package

Goal: make `run/` mean exactly "one run happens here."

Tasks:

- Create `run/`.
- Move run identity/status shapes to `run/runs.py`.
- Move thread scope/request shapes to `run/threads.py` and `run/thread_run.py`.
- Split `loop.py`:
  - main orchestration into `run/runtime.py`;
  - cancellation/deadline checks into `run/cancellation.py`;
  - result/outcome shapes into `run/outcomes.py`;
  - capability execution calls should use `capabilities/execution.py`;
  - model calls should use `models/`.
- Keep runtime invocation independent from CLI/RPC/worker.
- Ensure runtime writes events/provenance only through explicit ports.

Validation:

- Run-loop tests from `tests/test_zeta_agent.py`.
- Cancellation/deadline tests.
- Capability/tool-call tests.
- Model telemetry tests.
- Thread/run result tests.

### Phase 5: Split Orchestration

Goal: make event-driven work separate from the run loop and process wiring.

Tasks:

- Create `orchestration/`.
- Move agent definitions/invocations and authored-agent compilation to
  `orchestration/agents.py`.
- Split queue item shape/helpers into `orchestration/queue.py`.
- Split attempt shape/helpers into `orchestration/attempts.py`.
- Move event routing/lifecycle code from `dispatch.py` into
  `orchestration/dispatch.py`.
- Move worker claiming/lease/heartbeat behavior from `runtime/local.py` into
  `orchestration/worker.py`.
- Move schedule emission from `runtime/local.py` into
  `orchestration/scheduling.py`.
- Decide where `session.turn.requested` lives:
  - `run/thread_run.py` if it is direct run adaptation;
  - `orchestration/session_turn_agent.py` if it remains an event-triggered
    built-in agent.

Validation:

- Dispatch tests.
- Queue/attempt lifecycle tests.
- Worker run-once tests.
- Schedule emission tests.
- RPC session-run tests, because they observe terminal queue item results.

### Phase 6: Introduce Process

Goal: make local construction explicit and boring.

Tasks:

- Create top-level `process.py`.
- Move state dir resolution, event/object store opening, agent spec loading,
  worker construction, model selection, and RPC service construction there.
- Keep `process.py` high-level. If path/env logic grows, consider a local helper
  later, but do not add a package prematurely.
- Update CLI and RPC stdio entrypoints to request concrete services from
  `process.py`.

Validation:

- CLI smoke tests.
- RPC stdio tests.
- Worker construction tests.
- Model profile selection tests.

### Phase 7: Clean RPC And CLI Boundaries

Goal: keep interfaces as adapters, not behavior owners.

Tasks:

- Keep `zeta/cli.py` top-level and thin.
- Keep `rpc/jsonrpc.py`, `rpc/stdio.py`, and `rpc/routes.py`.
- Make route adapters call `process.py`, `run/`, `records/`, and
  `orchestration/` boundaries instead of reaching into old modules.
- Split `rpc/routes.py` only after behavior is stable and route groups are
  obvious.

Validation:

- RPC protocol tests.
- CLI command tests.
- End-to-end `session.run` tests.

### Phase 8: Remove Old Structure

Goal: make the repo match the story.

Tasks:

- Delete emptied `kernel/`, old `store/`, old broad `runtime/`, old `loop.py`,
  old `execute.py`, and old `dispatch.py` once imports are gone.
- Keep no long-lived compatibility modules unless a real external import
  contract is explicitly required.
- Update docs and README references.
- Re-run the full test suite and pre-commit.

Validation:

- `uv run pytest`
- `uv run pre-commit run --all`
- Optional complexity checks after the largest moves:
  - `uvx --with radon radon cc src tests -s`
  - repository complexity gate if still configured.

## Test Reorganization

The current tests should eventually mirror the target packages:

```text
tests/zeta/records/
tests/zeta/run/
tests/zeta/context/
tests/zeta/models/
tests/zeta/capabilities/
tests/zeta/orchestration/
tests/zeta/rpc/
tests/zeta/process/
tests/integration/
```

Suggested migration:

- Split `tests/test_zeta_agent.py` across `run`, `capabilities`,
  `orchestration`, and integration tests.
- Split `tests/test_zeta_prompt.py` into `context` and `records/provenance`.
- Split `tests/test_zeta_trace.py` into `records/timeline` and
  `records/provenance`.
- Keep provider payload tests under `models`.
- Keep RPC wire-shape tests under `rpc`.
- Keep Sigil workflow tests separate from Zeta runtime tests.

## Risks

- Import churn can hide behavior changes. Move in small phases and run focused
  tests after each phase.
- `execute.py` is ambiguous. Decide whether its core identity is direct thread
  execution or event-triggered built-in agent execution before moving it.
- `context/builder.py` currently mixes prompt construction and provenance.
  Split behavior carefully so prompt object ids remain stable where tests
  expect them.
- `runtime/local.py` mixes process setup, worker mechanics, scheduling, and
  event-log RPC handling. This should be split before deeper runtime changes.
- Model profile state mixes provider semantics with local active-selection
  files. Do not move it blindly; first decide what is model semantics and what
  is process-local state.
- Avoid adding abstract service layers. Introduce only the interfaces already
  implied by stores, models, capabilities, and process construction.

## Acceptance Criteria

The refactor is successful when:

- A new reader can open `src/zeta/` and predict where to change a behavior.
- Runtime executes one run and does not know its caller.
- Orchestration turns accepted events into work and does not know the model
  loop.
- Context builds model input and does not execute.
- Models and capabilities are visible extension seams.
- Records own durable facts, timeline, provenance, and persistence.
- CLI and RPC are thin adapters.
- `process.py` is the only high-level local assembly entrypoint.
- The full test suite and pre-commit pass.
