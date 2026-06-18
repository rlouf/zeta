# Zeta modular architecture

## Core invariant

There is one append-only fact log. Durable content lives in the substrate store.
Runtime, dispatch, context, history, and CLI code derive the views they need from
those facts and substrate objects without becoming new sources of truth.

The codebase should use domain modules directly:

- `zeta.events` owns durable facts and event stores.
- `zeta.substrate` owns content-addressed objects, refs, derivations, and
  substrate stores.
- `zeta.capabilities` owns callable host capabilities, projection, invocation,
  policy, and staged/direct effects.
- `zeta.loop` owns model/capability turn orchestration.
- `zeta.dispatch` owns event-triggered agents and work lifecycle.
- `zeta.agents` owns authored specs, resources, prompts, manifests, and return
  schemas.
- `zeta.context` owns prompt construction, message reconstruction, budgeting,
  and transforms.
- `zeta.models` owns profile selection and protocol-agnostic model calls;
  transport-specific helpers live in `zeta.models.chat_completions` or
  `zeta.models.responses`.
- `zeta.history` owns human-facing turn/effect records and costs.
- `zeta.rpc` owns JSON-RPC protocol DTOs and transport glue.

There should be no duplicate public modules for old names.

## Comparison with `../zeta`

The Python layout mirrors the Rust workspace boundaries without copying Rust
crate structure mechanically:

| Rust crate | Python boundary | Ownership |
|---|---|---|
| `zeta-events` | `zeta/events/` | durable event shape, cursors, filters, append/list stores |
| `zeta-substrate` | `zeta/substrate/` | content-addressed objects, refs, derivations, store backends |
| `zeta-session` | `zeta/loop.py` plus context/model dependencies | model/capability loop orchestration over events and substrate |
| `zeta-dispatch` | `zeta/dispatch/` | event-triggered agents, work lifecycle, subscriptions, retries |
| `zeta-agents` | `zeta/agents/` | authored specs, prompts, resources, manifests, return schemas |
| `zeta-tools` | `zeta/capabilities/` | callable host capabilities, registry, descriptors, execution policy |
| `zeta-protocol` / daemon DTOs | `zeta/rpc.py` for now | protocol-shaped RPC DTOs and transport boundary |

## Target repository structure

```text
src/
  zeta/
    __init__.py
    loop.py

    events/
      __init__.py
      event.py
      payloads.py
      store.py
      sqlite.py
      memory.py
      timeline.py

    substrate/
      __init__.py
      object.py
      refs.py
      derivation.py
      links.py
      store.py
      sqlite.py

    agents/
      __init__.py
      spec.py
      loader.py
      prompts.py
      resources.py
      manifest.py
      events.py
      returns.py
      capabilities.py
      runtime.py

    dispatch/
      __init__.py
      dispatcher.py

    context/
      __init__.py
      budget.py
      builder.py
      components.py
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
      base.py
      registry.py

    history.py
    rpc.py
    session.py
    skills.py
```

Modules that should not exist:

```text
src/zeta/tools/
src/zeta/trace.py
src/zeta/timeline.py
src/zeta/turn.py
```

## Ownership boundaries

### `events/`

`events/` owns the durable event ontology and append-only log API:

- `Event`, `DraftEvent`, `EventCursor`, and `AppendOutcome`
- typed event payload constructors and validators
- cursors, filters, idempotency keys, and timestamp helpers
- append/list protocols
- SQLite and in-memory event stores
- durable timeline projection over the event log

It should not own substrate storage, dispatch, model prompts, capability
invocation, or context reconstruction.

### `substrate/`

`substrate/` owns immutable content-addressed values, refs, and derivations:

- object ids and canonical object hashing
- put/get/search/closure protocols
- refs and compare-and-swap semantics
- derivations and object graph queries
- event-to-object links such as "model call used prompt X and produced
  assistant message Y"
- concrete substrate stores

Operational facts such as timestamps, retries, worker identity, and latency
belong in events, not substrate objects.

### `capabilities/`

`capabilities/` owns the runtime capability abstraction:

- capability ids and descriptors
- effect kinds and execution policy
- staged/direct execution support
- registry lookup and model projection
- argument validation and invocation

Model-provider "tool call" vocabulary stays in provider-facing prompt/loop code
where it describes the API protocol.

### `loop.py`

`loop.py` owns one model/capability turn:

- prompt request construction
- model calls
- capability calls
- runtime fact sequencing
- cancellation and deadline checks
- final answer/staged-effect outcome

It emits durable event-shaped dictionaries at the event boundary, but it should
not own event storage, substrate persistence, authored-agent spec parsing, or
CLI presentation.

### `dispatch/`

`dispatch/` owns event-triggered execution:

- trigger rules
- agent definitions used by dispatch
- pending/claimed/completed/failed work events
- duplicate suppression through idempotency keys
- runner result collection

It should not own authored-agent file formats. Authored specs compile into
dispatch definitions.

### `agents/`

`agents/` owns authored-agent definitions:

- frontmatter parsing and validation
- recursive spec loading
- prompt rendering and template validation
- resource and skill loading
- manifest validation
- accepted/returned event vocabulary
- return schema derivation
- conversion from spec to dispatch definition

### `context/`

`context/` owns model input construction:

- context components
- durable-event-to-chat-message reconstruction
- prompt object storage and reconstruction
- context budget measurement
- compaction transforms
- project/user instruction loading
- system prompt rendering

### `models/`

`models/` owns model profile selection and protocol-agnostic calls:

- profile loading and resolution
- active model selection
- `chat_completion_messages`
- `chat_structured_output`

Transport helpers such as endpoint probing, request-body construction, and
OpenAI-compatible streaming stay in `models/chat_completions.py`.

### `history.py`

`history.py` owns human-facing turn and effect records:

- turn ids
- workflow/outcome/cost fields
- effect records
- touched files
- pending staged effects
- log/blame/export read models

It remains a read model over durable events, not a second authoritative store.

## Durable event names

The module cleanup does not rename persisted event schemas. Names such as
`zeta.turn.completed`, `zeta.turn.failed`, `zeta.turn.aborted`, and
`zeta.turn` are durable event contracts, not Python module import paths.

## Test strategy

Use tests to preserve behavior while keeping ownership clean:

- event append ordering, cursor/filter behavior, and idempotency keys
- dispatch matching, duplicate suppression, and work-event creation
- substrate object identity, ref compare-and-swap, derivation traversal, and
  graph closure
- prompt reconstruction from durable events and substrate objects
- capability projection and staged/direct execution policy
- authored-agent spec parsing, resource loading, manifest validation, and return
  schema derivation
- loop cancellation/deadline behavior and model/capability progression

For non-trivial Python changes:

- run `ripple` before modifying a function and account for callers
- run targeted tests first, then `uv run pytest -q`
- run `uvx --with radon radon cc src tests -s`
- run coverage when behavior changes
- run `uv run pre-commit run --all` when documentation changes
