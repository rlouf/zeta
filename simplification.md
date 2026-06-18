# Zeta modular architecture plan

## Core invariant

There is one append-only fact log. Durable content lives in the substrate store.
Runtime, dispatch, context, history, and CLI code derive the views they need from
those facts and substrate objects without becoming new sources of truth.

The event log module can stay named `events` at the public boundary. That name
is already established in the repository and is clearer for callers than
introducing both `events` and `log`. Internally, `events/` owns event envelopes,
typed payload schemas, cursors, filters, idempotency rules, and append/list
protocols.

Use `substrate/`, not `artifacts/`. This matches `../zeta/zeta-substrate` and
keeps the layer conceptually broader than user-facing artifacts: it owns
immutable objects, refs, derivations, freshness, and object graph queries.

Use `capabilities/`, not `tools/`, for the Python runtime boundary. The existing
`tools/` package remains as a thin compatibility and model-API vocabulary layer.
The capability layer owns descriptors, projection, invocation, execution policy,
and staged/direct effects.

## Comparison with `../zeta`

The target Python layout should mirror the Rust workspace boundaries without
copying Rust crate structure mechanically:

| Rust crate | Python boundary | Ownership |
|---|---|---|
| `zeta-events` | `zeta/events/` | durable event shape, cursors, filters, append/list stores |
| `zeta-substrate` | `zeta/substrate/` | content-addressed objects, refs, derivations, store backends |
| `zeta-session` | `zeta/loop.py` plus context/model dependencies | model/capability loop orchestration over events and substrate |
| `zeta-dispatch` | `zeta/dispatch/` | event-triggered agents, work lifecycle, subscriptions, retries |
| `zeta-agents` | `zeta/agents/` | authored specs, prompts, resources, manifests, return schemas |
| `zeta-tools` | `zeta/capabilities/` | callable host capabilities, registry, descriptors, execution policy |
| `zeta-protocol` / daemon DTOs | `zeta/rpc.py` for now | protocol-shaped RPC DTOs and transport boundary |

The current Python code already contains these concepts, but several modules
combine multiple ownership boundaries:

- `events.py` mixes durable event storage with dispatch types and agent routing.
- `trace.py` is really the substrate store, plus trace-specific helpers.
- `timeline.py` mixes durable event recording, event-to-message conversion,
  substrate link extraction, and timeline projection.
- `agents.py` combines spec parsing, resource loading, prompt rendering,
  manifests, event validation, return schema derivation, and dispatch adapters.
- `turn.py` owns the loop, but still works with timeline-shaped dictionaries and
  imports trace/tool concepts directly.

## Target repository structure

```text
src/
  zeta/
    __init__.py

    loop.py                # model/capability loop orchestration

    events/
      __init__.py          # public re-exports during migration
      event.py             # Event, DraftEvent, EventCursor, AppendOutcome
      types.py             # event type constants and ontology names
      payloads.py          # typed payload records / validators
      store.py             # event reader/sink/store protocols and filters
      sqlite.py            # SQLite event store
      memory.py            # in-memory event store

    substrate/
      __init__.py          # public re-exports during migration
      object.py            # content-addressed objects and object ids
      refs.py              # mutable refs and compare-and-swap semantics
      derivation.py        # provenance / derivation records
      store.py             # substrate store protocols
      sqlite.py            # SQLite substrate store
      memory.py            # in-memory substrate store

    agents/
      __init__.py
      spec.py              # authored agent spec/frontmatter types
      loader.py            # load agent specs from disk
      prompts.py           # render/validate authored prompt templates
      resources.py         # skills, AGENTS.md, project/user resources
      manifest.py          # validate specs against deployment vocabulary
      events.py            # authored-agent event vocabulary validation
      returns.py           # return-event schema derivation
      capabilities.py      # capability declarations in specs

    dispatch/
      __init__.py
      dispatcher.py        # event matching and work creation
      triggers.py          # trigger rules and subscriptions
      work.py              # work ids and pending/claimed/completed/failed facts
      scheduler.py         # scheduled event emission
      retry.py             # retry policy for dispatched work

    context/
      __init__.py
      budget.py
      builder.py           # public owner of prompt/model-input construction
      components.py        # component-level prompt representation
      instructions.py
      system.py
      transforms.py

    models/
      __init__.py
      chat_completions.py
      codex_auth.py
      profiles.py
      responses.py

    capabilities/
      __init__.py
      base.py              # capability contracts, result/effect types, policy fields
      registry.py          # registration, projection, invocation

    history.py             # human/CLI turn records, effects, costs
    rpc.py                 # JSON-RPC / protocol-shaped boundary
    session.py             # session wiring and default stores
```

There should not be a `zeta/compat/` package. Compatibility lives at the old
public import paths when those names remain useful to callers:

```text
src/zeta/trace.py          # deprecated wrapper around substrate/*
src/zeta/timeline.py       # deprecated wrapper around events/context/substrate
src/zeta/tools/            # public model-tool vocabulary over capabilities/*
src/zeta/turn.py           # compatibility alias for loop.py
```

Internal imports should use the owner modules directly. Public wrappers should
not gain new behavior.

## Ownership boundaries

### `events/`

`events/` owns the durable event ontology and append-only log API:

- `Event`
- `DraftEvent`
- typed event payloads
- cursors
- filters
- idempotency rules
- event type/payload schemas
- append/list protocols
- concrete event stores

It should not own substrate storage, agent dispatch, model prompts, capability
invocation, or projection logic.

### `substrate/`

`substrate/` owns immutable content-addressed values, refs, and derivations:

- object ids
- object shape
- canonical object hashing
- put/get/search/closure protocols
- refs and compare-and-swap semantics
- derivations
- freshness checks against refs
- event-to-object links such as "model call used prompt X and produced
  assistant message Y"
- concrete substrate stores

Operational facts such as timestamps, retries, worker identity, and latency
belong in events, not substrate objects.

### `dispatch/`

`dispatch/` owns event-triggered execution:

- trigger rules
- subscriptions
- agent definitions used by dispatch
- work pending/claimed/completed/failed events
- retry policy for dispatched work
- scheduled event emission
- dispatch outcomes

Cancellation/deadline helpers should stay with `loop.py` when they are about
cooperative model/capability loop execution. They belong in `dispatch/` only
when they are specifically about cancelling dispatched work.

### `loop.py`

`loop.py` owns the agent loop:

- build prompt
- call model
- record assistant output
- request and resolve capabilities
- emit typed runtime facts

It should orchestrate storage through protocols and should not know SQLite
details. Rename `turn.py` to `loop.py` only after surrounding imports are ready.

### `agents/`

`agents/` owns the declarative authored-agent layer:

- agent spec/frontmatter parsing
- spec loading from disk
- authored prompt template rendering and validation
- manifest validation against event and capability vocabularies
- return-event schema derivation
- prompt resources such as skills and AGENTS.md/project/user files

This mirrors `../zeta/zeta-agents`. Runtime/session/context code should consume
validated specs, rendered prompts, and loaded resources; it should not know how
Markdown agent files or skills are discovered.

### `capabilities/`

`capabilities/` owns the capability subsystem:

- provider-facing descriptors
- model-requested calls
- normalized outputs
- error shapes
- registry and projection
- execution mode and trust policy
- staged/direct effect policy
- host/client adapters

It should not grow a miscellaneous collection of built-in implementations.
Concrete host actions should live behind adapters or outside the substrate-facing
Zeta core.

### `context/`

`context/builder.py` remains the public owner of prompt/model-input
construction. It coordinates prompt components, capability descriptors, context
text, budget, and model-facing rendering.

`context/components.py` owns the component-level representation used by the
builder. The model-facing parts of `timeline.py` should move into the existing
context package only where they are actually prompt/component concerns:

- durable events to model transcript
- tool-call/tool-result reconciliation
- message-boundary trimming
- prompt component source data

Do not add `context/messages.py` as an architectural boundary by default.
`context/builder.py` and `context/components.py` already own prompt/model-input
construction. Add a helper module only if it removes real duplication or keeps
`components.py` from becoming unclear.

### `history.py`

`history.py` owns the human-facing turn/effect read model:

- turn records
- effect records
- cost summaries
- touched files
- pending staged effects

It should remain a read model over durable events, not a second authoritative
store.

## `timeline.py` end state

`timeline.py` should not exist in the end state. Chronological ordering is a
property of the event log, not a module ownership boundary.

Current timeline responsibilities should move as follows:

- event-shaped dict enrichment and append helpers -> `events/`
- durable event read/list helpers -> `events/`
- substrate id/link helpers -> `substrate/`
- latest event time queries -> `events/`
- model-message conversion -> `context/builder.py` / `context/components.py`
- tool-call/tool-result reconciliation for prompts -> `context/`
- human-facing turn/effect projection -> `history.py`

Keep a temporary `timeline.py` wrapper only while internal imports are still
moving.

## Migration principle

Do not rewrite around the target tree in one step. Move ownership first while
preserving public behavior and tests.

1. Replace flat `events.py` with an `events/` package, re-exporting the current
   public names from `events/__init__.py` while callers migrate.
2. Move event SQLite and memory stores under `events/`.
3. Extract dispatch types and `EventDispatcher` into `dispatch/`.
4. Extract trace object/ref/derivation concepts into `substrate/`, keeping
   `trace.py` as a temporary public wrapper.
5. Move event recording/reading helpers out of `timeline.py` into `events/`.
6. Move model-facing message conversion out of `timeline.py` into the existing
   context builder/component path, adding a helper module only if it removes real
   duplication.
7. Move substrate object-link helper functions out of `timeline.py`.
8. Delete internal imports from `timeline.py`, then delete `timeline.py` after
   public compatibility is no longer needed.
9. Keep human-facing turn/effect records in `history.py`, while making sure it
   remains a read model over durable events.
10. Move agent specs, prompt templates, resources, and skill loading into
    `agents/`, matching the `../zeta/zeta-agents` boundary.
11. Move `tools/` implementation into `capabilities/`, leaving `tools/` as a
    temporary wrapper.
12. Rename `turn.py` to `loop.py` once the surrounding imports are ready.
13. Convert `loop.py` internals from loose dict events to typed runtime facts,
    while serializing to existing durable/event-shaped dictionaries at the
    boundary.
14. Delete temporary wrappers only after all internal imports and tests are
    migrated.

## Commit plan

Each section below is one intended commit. Keep each commit behavior-preserving
unless the section explicitly says otherwise. Start with tests, then move code,
then run the targeted tests before moving to the next commit.

### Commit 1: Lock current event and dispatch contracts

- [x] Add focused tests for `EventDispatcher` behavior before moving it:
  matching by exact event type;
  matching by event type prefix;
  no work creation for duplicate idempotency hits;
  work event causal links;
  agent runner result collection;
  publish callback ordering.
- [x] Add or identify existing tests for durable event store behavior:
  append ordering;
  idempotency;
  cursor decoding;
  filters by session, turn, type, prefix, and cause.
- [x] Run `ripple` on `zeta.events.EventDispatcher.dispatch`.
- [x] Run the ripple-listed tests and the event/dispatch-focused tests.
- [x] Do not move code in this commit unless tests need tiny fixtures.

### Commit 2: Split dispatch out of `events.py`

- [x] Create `src/zeta/dispatch/`.
- [x] Move `TriggerRule`, `AgentDefinition`, `AgentRun`, `DispatchOutcome`, and
  `EventDispatcher` into the new package.
- [x] Keep behavior identical; this is an ownership move only.
- [x] Re-export moved names from the old `zeta.events` public path.
- [x] Update internal imports where the ownership boundary is clear:
  `agents.py` and `rpc.py` should import dispatch concepts from
  `zeta.dispatch`.
- [x] Leave tests importing old public paths if that helps prove compatibility.
- [x] Run the ripple-listed tests from Commit 1.
- [x] Run `uv run pytest tests/test_zeta_agents.py tests/test_zeta_agent.py -q`.

### Commit 3: Convert `events.py` into an `events/` package

- [x] Create `src/zeta/events/`.
- [x] Move event envelope and cursor types into `events/event.py`:
  `Event`, `DraftEvent`, `EventCursor`, `AppendOutcome`.
- [x] Move store protocols and filters into `events/store.py`:
  `EventSink`, `EventReader`, `Filter`.
- [x] Move SQLite store and path helpers into `events/sqlite.py`.
- [x] Move event type constructors and ontology constants into
  `events/types.py` or `events/payloads.py`, depending on whether they are type
  names or payload builders.
- [x] Populate `events/__init__.py` with public re-exports matching the old
  `zeta.events` import surface.
- [x] Delete the old flat `events.py` only after imports resolve against the new
  package.
- [x] Run `uv run pytest tests/test_security_state.py tests/test_zeta_trace.py tests/test_history.py -q`.
- [x] Run `uv run pytest -q`.

### Commit 4: Add in-memory event store only if existing tests need it

- [x] Check whether a real in-memory event store already exists or whether tests
  can use list-backed sinks.
- [x] If needed, add `events/memory.py` with the same append/list semantics as
  SQLite for tests and ephemeral sessions.
- [x] Do not introduce a generic fake framework.
- [x] Add tests that compare memory and SQLite behavior for idempotency,
  ordering, and filters.
- [x] Run `uv run pytest tests/test_security_state.py tests/test_zeta_agent.py -q`.

### Commit 5: Lock substrate behavior before extraction

- [x] Add or identify tests for current `trace.py` substrate behavior:
  canonical object identity;
  object retrieval;
  prefix resolution;
  ambiguous and unknown ids;
  refs;
  compare-and-swap ref conflicts;
  derivations by input/output;
  graph closure;
  search;
  stats.
- [x] Run `ripple` on key functions before moving them:
  `resolve_object_id`,
  `SqliteStore.put_object`,
  `SqliteStore.set_ref`,
  `SqliteStore.record_derivation`.
- [x] Run `uv run pytest tests/test_zeta_trace.py tests/test_zeta_prompt.py -q`.
- [x] Do not move code in this commit unless tests need tiny fixtures.

### Commit 6: Extract `substrate/` from `trace.py`

- [x] Create `src/zeta/substrate/`.
- [x] Move `Object`, `ObjectId`, and canonical object hashing into
  `substrate/object.py`.
- [x] Move ref names and compare-and-swap semantics into `substrate/refs.py`.
- [x] Move `Derivation` and derivation lookup semantics into
  `substrate/derivation.py`.
- [x] Move the store protocol into `substrate/store.py`.
- [x] Move SQLite substrate storage into `substrate/sqlite.py`.
- [x] Add `substrate/__init__.py` re-exporting the stable substrate surface.
- [x] Keep `trace.py` as a compatibility wrapper around `substrate/` plus
  trace-specific prompt helpers.
- [x] Update internal imports in `context/`, `turn.py`, `session.py`, and
  `timeline.py` to use `zeta.substrate` where the concept is substrate-owned.
- [x] Keep user-facing trace CLI names unchanged.
- [x] Run `uv run pytest tests/test_zeta_trace.py tests/test_zeta_prompt.py tests/test_workflows.py -q`.

### Commit 7: Move prompt-trace helpers to the right owner

- [x] Decide whether `PromptTrace`, `prompt_trace_payload`, and
  `latest_prompt_trace_fields` are substrate-level provenance helpers or
  context-level prompt helpers.
- [x] Move them out of the generic `trace.py` wrapper to the chosen owner.
- [x] Update imports in `context/builder.py`, `turn.py`, and workflow code.
- [x] Keep `trace.py` re-exporting old names temporarily.
- [x] Run `uv run pytest tests/test_zeta_prompt.py tests/test_zeta_agent.py -q`.

### Commit 8: Move durable timeline event helpers into `events/`

- [x] Move timeline-to-durable event conversion into `events/payloads.py`:
  durable type mapping;
  durable payload shaping;
  timestamp conversion;
  event id derivation.
- [x] Move durable event append/read helpers into `events/`:
  `record_durable_event`;
  current event listing helpers;
  latest event time queries.
- [x] Keep `timeline.py` wrappers for old function names.
- [x] Update `rpc.py` and `history.py` imports where they are clearly event-owned.
- [x] Run `uv run pytest tests/test_history.py tests/test_zeta_trace.py tests/test_workflows.py -q`.

### Commit 9: Move substrate link extraction out of `timeline.py`

- [x] Move object-link extraction helpers to `substrate/`:
  `durable_event_object_links`;
  `add_object_link`;
  `add_object_links`;
  `trace_object_id` if it remains substrate-shaped.
- [x] Keep only compatibility wrappers in `timeline.py`.
- [x] Update `context/components.py` and event payload code to import from the
  new owner.
- [x] Run `uv run pytest tests/test_zeta_prompt.py tests/test_zeta_trace.py -q`.

### Commit 10: Move model-message conversion into `context/`

- [x] Move `ChatMessageEntry`, `_chat_message_entries`,
  `from_message_boundary`, and related tool-call/tool-result reconciliation into
  `context/components.py` or a small private context helper if
  `components.py` becomes unclear.
- [x] Keep `context/builder.py` as the public prompt construction owner.
- [x] Remove context imports from `timeline.py` once wrappers are no longer
  needed internally.
- [x] Run `uv run pytest tests/test_zeta_prompt.py tests/test_zeta_agent.py -q`.

### Commit 11: Delete internal dependencies on `timeline.py`

- [x] Use structural search to find all internal imports from `zeta.timeline`.
- [x] Replace them with imports from `events/`, `substrate/`, `context/`, or
  `history.py`.
- [x] Keep `timeline.py` only as a public compatibility wrapper.
- [x] Run `uv run pytest tests/test_zeta_prompt.py tests/test_zeta_trace.py tests/test_zeta_agent.py tests/test_workflows.py -q`.

### Commit 12: Split authored agents into an `agents/` package

- [x] Add tests or identify existing tests for:
  spec loading;
  frontmatter validation;
  schedules;
  prompt rendering;
  undefined template variables;
  event vocabulary validation;
  capability vocabulary validation;
  return schema derivation;
  resource and skills loading.
- [x] Create `src/zeta/agents/`.
- [x] Move spec dataclasses and frontmatter parsing into `agents/spec.py`.
- [x] Move recursive disk loading into `agents/loader.py`.
- [x] Move prompt rendering and validation into `agents/prompts.py`.
- [x] Move resource and skill loading into `agents/resources.py`.
- [x] Move deployment manifest validation into `agents/manifest.py`.
- [x] Move event vocabulary validation into `agents/events.py`.
- [x] Move return schema derivation into `agents/returns.py`.
- [x] Move spec capability declarations and validation into
  `agents/capabilities.py`.
- [x] Keep `agents/__init__.py` re-exporting the old `zeta.agents` surface.
- [x] Run `uv run pytest tests/test_zeta_agents.py tests/test_zeta_agent.py -q`.

### Commit 13: Introduce `capabilities/` beside existing `tools/`

- [x] Create `src/zeta/capabilities/`.
- [x] Move capability result/effect contracts from `tools/base.py` into
  `capabilities/base.py`.
- [x] Move registry and projection from `tools/registry.py` into
  `capabilities/registry.py`.
- [x] Move execution mode and trust/staging policy into
  `capabilities/policies.py` if this removes real ownership confusion.
- [x] Add `capabilities/adapters.py` only for actual host/client adapter code,
  not as a placeholder.
- [x] Keep `tools/` as wrappers re-exporting the new capability implementation.
- [x] Update internal imports in `turn.py`, `context/`, and `agents/` to use
  `zeta.capabilities`.
- [x] Leave tests on old imports where they prove compatibility.
- [x] Run `ripple` on `CapabilityRegistry.project`.
- [x] Run `uv run pytest tests/test_zeta_tools.py tests/test_zeta_agent.py tests/test_zeta_prompt.py -q`.

### Commit 14: Rename tests and public wording around capabilities

- [x] Rename test module and test names from tool language to capability
  language where they exercise the generic subsystem.
- [x] Keep user-facing "tool call" wording where it describes model API
  semantics.
- [x] Update documentation or help text only if the product vocabulary should
  change; otherwise keep this commit code/test-only.
- [x] Run `uv run pytest tests/test_zeta_tools.py tests/test_zeta_agent.py -q`
  or the renamed equivalent.

### Commit 15: Rename `turn.py` to `loop.py`

- [x] Run `ripple` on `run_agent_turn` and the main step functions.
- [x] Move `turn.py` to `loop.py`.
- [x] Keep `turn.py` as a temporary wrapper re-exporting the old public names.
- [x] Update internal imports to `zeta.loop`.
- [x] Keep compatibility tests for `zeta.turn` imports if external use matters.
- [x] Run the ripple-listed tests.
- [x] Run `uv run pytest tests/test_zeta_agent.py tests/test_workflows.py -q`.

### Commit 16: Introduce typed loop runtime facts

- [x] Identify the concrete loose dict event shapes emitted by the loop:
  model call;
  model usage;
  capability call;
  capability result;
  staged effect;
  abort;
  finish.
- [x] Add typed records for those runtime facts.
- [x] Convert loop internals to pass typed facts between steps.
- [x] Serialize typed facts to durable event-shaped dictionaries only at the
  event sink boundary.
- [x] Keep external event payloads unchanged unless the tests and migration plan
  explicitly say otherwise.
- [x] Run `ripple` on each changed loop function before editing.
- [x] Run ripple-listed tests, then
  `uv run pytest tests/test_zeta_agent.py tests/test_zeta_prompt.py tests/test_workflows.py -q`.
- [x] Run `uvx --with radon radon cc src tests -s`.

### Commit 17: Remove `timeline.py` compatibility

- [x] Confirm no internal imports from `zeta.timeline` remain.
- [x] Decide whether external compatibility is still needed.
- [x] If not needed, delete `timeline.py`.
- [x] If still needed, leave it but mark it as deprecated in the module
  docstring and do not add new behavior there.
- [x] Run `uv run pytest -q`.

### Commit 18: Remove `trace.py` compatibility or narrow it to CLI naming

- [x] Confirm all internal substrate imports use `zeta.substrate`.
- [x] Decide whether `zeta.trace` is still a public API or only a CLI concept.
- [x] If public compatibility is not needed, delete `trace.py`.
- [x] If trace CLI names stay public, keep only CLI/user-facing trace helpers
  there and no substrate implementation.
- [x] Run `uv run pytest tests/test_zeta_trace.py tests/test_workflows.py -q`.

### Commit 19: Remove `tools/` compatibility

- [x] Confirm all internal generic capability imports use `zeta.capabilities`.
- [x] Decide whether `zeta.tools` remains public because model APIs call these
  "tools".
- [x] If not needed, delete `tools/`.
- [x] If kept, make it a thin compatibility/public-vocabulary package only.
- [x] Run `uv run pytest tests/test_zeta_tools.py tests/test_zeta_agent.py -q`
  or the renamed equivalent.

### Commit 20: Final architecture cleanup and documentation

- [x] Run structural searches for old ownership leaks:
  dispatch names imported from `events`;
  substrate names imported from `trace`;
  capability names imported from `tools`;
  timeline imports;
  SQLite details imported by loop/context code.
- [x] Remove stale wrappers, comments, and TODOs that no longer apply.
- [x] Update architecture docs to describe the final ownership boundaries.
- [x] Run `uv run pytest -q`.
- [x] Run `uvx --with radon radon cc src tests -s`.
- [x] Run coverage for the final behavior-preserving refactor:
  `uvx --with coverage coverage run -m pytest`
  and `uvx --with coverage coverage report`.
- [x] Because documentation changed, run
  `uv run pre-commit run --all`.

## Safer implementation sequence

The proposal is an end state. The safest first steps are narrower:

### Stage 1: Event and dispatch split

- Add tests that lock current `EventDispatcher` behavior.
- Move `TriggerRule`, `AgentDefinition`, `AgentRun`, `DispatchOutcome`, and
  `EventDispatcher` into `dispatch/`.
- Re-export them from the old event import path temporarily.
- Keep durable event store behavior unchanged.

### Stage 2: Authored-agent split

- Split `agents.py` into `agents/spec.py`, `loader.py`, `prompts.py`,
  `resources.py`, `manifest.py`, `events.py`, `returns.py`, and
  `capabilities.py`.
- Keep `agents/__init__.py` re-exporting the current public names.
- Do not change runtime behavior in this stage.

### Stage 3: Substrate extraction

- Move `Object`, `ObjectId`, `Derivation`, `PromptTrace`, refs, and store
  protocols from `trace.py` into `substrate/`.
- Keep `trace.py` as a deprecated wrapper around `substrate/`.
- Keep trace-specific CLI naming only at user-facing boundaries.

### Stage 4: Timeline demolition

- Move durable event append/read helpers into `events/`.
- Move object-link derivation into `substrate/`.
- Move model-message reconstruction into `context/`.
- Keep only deprecated compatibility wrappers in `timeline.py`.

### Stage 5: Capability rename

- Move `tools/base.py` and `tools/registry.py` implementation into
  `capabilities/`.
- Keep `tools/` as a public model-tool vocabulary wrapper.
- Rename generic test names to capability language while keeping builtin tool
  tests under `test_zeta_tools.py`.

### Stage 6: Loop rename and typed facts

- Rename `turn.py` to `loop.py`.
- Keep public wrappers if needed.
- Replace loose runtime event dictionaries inside the loop with typed facts.
- Serialize to durable event dictionaries at the event-store boundary.

## Naming decisions

- Use `events/`, not `log/`.
- Keep event schemas and log protocols together under `events/`.
- Use `substrate/`, not `artifacts/`.
- Use `capabilities/`, not `tools/`, for the long-term Python runtime boundary.
- Keep public wrappers at old public import paths when they preserve useful
  external vocabulary.
- Do not add `zeta/compat/`.
- Keep `timeline.py` deprecated unless a later public API cleanup removes it.
- Do not add `context/messages.py` by default.
- Keep `history.py` for human/CLI turn records unless a later split has a clear
  benefit.

## Test strategy

Start each step with tests that preserve concrete behavior before moving code.

- For Python function changes, run `ripple` first and account for every caller.
- After modifying a function, run the ripple-listed tests first, then the full
  suite.
- Use `uv` for Python commands.
- Run `radon` after non-trivial Python edits:
  `uvx --with radon radon cc src tests -s`.
- Run coverage for non-trivial behavior changes:
  `uvx --with coverage coverage run -m pytest`
  and `uvx --with coverage coverage report`.
- Run `pre-commit` only if documentation is updated.

Useful contract tests to keep or add around the migration:

- event append ordering, cursor/filter behavior, and idempotency keys;
- dispatch matching, duplicate suppression, and work-event creation;
- substrate object identity, ref compare-and-swap, derivation traversal, and
  graph closure;
- prompt reconstruction from durable events and substrate objects;
- capability projection and staged/direct execution policy;
- authored-agent spec parsing, resource loading, manifest validation, and return
  schema derivation;
- loop cancellation/deadline behavior and model/capability progression.

## Decisions left for later

- Whether `rpc.py` should remain a single module or split protocol DTOs after
  the core ownership refactor.
- Whether a future public API cleanup should remove deprecated import paths
  after downstream callers have migrated.
