# Zeta event/runtime simplification plan

## Goal

Simplify Zeta around two internal event representations:

- `DraftEvent`: a produced event that may be persisted or live-only;
- `Event`: a durable ledger fact returned by the event store.

Plain dictionaries should be boundary/view shapes only. They are acceptable for
model provider payloads, JSON-RPC messages, CLI display, and tests that assert
external behavior. They should not be the core runtime event representation.

The refactor should remove files and concepts where possible:

- fold `runtime_events.py` into `events.py`;
- delete `timeline.py`;
- avoid replacing them with new one-function modules;
- keep conversion/projection helpers boring and close to the event model.
- aggressively remove legacy and compatibility code instead of preserving old
  call paths.
- inline helpers when they become very small and have only one or two local
  callers.

Compatibility with old internal APIs is not a goal. If an old helper only
exists to preserve a previous internal import path or event conversion path,
delete it and update callers.

## Why

The current implementation has too many event shapes:

- runtime/client dictionaries such as `{"type": "tool_result", ...}`;
- typed runtime event wrappers in `runtime_events.py`;
- `DraftEvent`;
- durable `Event`;
- projected timeline dictionaries.

This creates repeated conversions across `loop.py`, `runtime_events.py`,
`session.py`, `timeline.py`, and `rpc.py`. It also blurs ownership: RPC still
knows durable event naming, `runtime_events.py` imports from `loop.py`, and
`timeline.py` knows about the session shape.

The intended simpler rule is:

```text
loop emits DraftEvent
session persists DraftEvent -> Event
events.py projects Event/DraftEvent -> boundary dict when needed
rpc serializes boundary dicts
```

## Target responsibilities

### `events.py`

Owns:

- `DraftEvent`;
- `Event`;
- `Filter`;
- append outcome and sink protocols;
- immutable payload handling;
- canonical event view projection;
- runtime draft constructors.

Examples of constructors that should live here after folding
`runtime_events.py`:

- `model_call_draft(...)`;
- `tool_call_started_draft(...)`;
- `tool_call_completed_draft(...)`;
- `tool_call_failed_draft(...)`;
- `turn_aborted_draft(...)`;
- `stream_chunk_draft(...)`;
- `status_update_draft(...)`;
- `user_message_draft(...)` if useful.

The projection helpers should be mechanical:

- `event_view(event: Event) -> dict[str, Any]`;
- `draft_event_view(draft: DraftEvent) -> dict[str, Any]`.

They may:

- expose `type`, `id`, `time`, `session`, `turn_id`, `caused_by`;
- expose `cursor` for durable events with `seq`;
- use `_timeline_type` or another internal payload marker as a view type
  override;
- strip internal payload keys such as `_timeline_type` and `_time`.

They should not know about RPC envelopes, JSON-RPC error shapes, model
transport requests, or session workflow policy.

### `store/events/`

Owns event persistence and protocols only.

The store accepts `DraftEvent` and returns `Event`. It should not project events
for model/RPC/UI consumers.

Legacy convenience APIs should be removed when their callers are migrated:

- `append_event_to_log`;
- `append_event_to_log_outcome`;
- `publish_event_to_log`;
- `read_event_log`;
- `event_log_children`;
- `event_log_causal_chain`;
- `event_log_turn_events`.

Keep only store classes and protocols unless a function has an active,
non-compatibility caller after the refactor.

### `loop.py`

Owns one turn of execution:

- build model input;
- call the model gateway;
- execute or stage capabilities;
- emit `DraftEvent` values;
- return `AgentTurnResult(events=list[DraftEvent])`.

It should not:

- create durable `Event` values;
- import projection helpers for durable events;
- emit raw event dictionaries as internal runtime events.

Provider message dictionaries are still fine. They are model transport payloads,
not runtime events.

### `session.py`

Owns workflow/session policy:

- parse session requests;
- map `ask` / `propose` / `do` to execution mode;
- build `AgentConfig`;
- read prior durable events for model context;
- call `run_agent_turn`;
- persist returned `DraftEvent` values when durable;
- publish event views.

It should call `event_view(...)` / `draft_event_view(...)` from `events.py` when
it needs a boundary dict.

It should not own generic projection helpers.

### `rpc.py`

Owns JSON-RPC transport:

- parse messages;
- write responses, errors, and notifications;
- manage request IDs;
- manage async run lifecycle and cancellation;
- manage subscriptions;
- bridge client tools over RPC.

It should not know durable event naming or idempotency policy. For
`events.publish`, it should validate transport params, delegate event draft
construction to `events.py` or `session.py`, dispatch/persist the draft, and
serialize the resulting `event_view(...)`.

RPC should not re-export session helpers. Imports like
`from zeta.rpc import session_event_dispatcher` should be fixed at the caller
and the compatibility path should be deleted.

### `dispatch/`

Owns append-and-route behavior:

- accept incoming `DraftEvent`;
- publish durable `Event`;
- match registered agents;
- create durable work lifecycle events.

This can keep creating `DraftEvent` values for work lifecycle records because
those are dispatch-owned facts.

### `agents/`

Owns authored agent specs and compilation to `dispatch.AgentDefinition`.

No major structural change is required for this refactor, except keeping it on
the top side of the dependency graph.

### `context/`

Owns prompt construction and compaction.

This refactor should only touch it where `timeline.py` removal requires changing
how historical events are projected into model context.

Do not preserve old timeline-shaped context helpers unless they are directly
needed for model behavior.

## Legacy removal policy

When this refactor touches an internal API, prefer deletion over shims.

Delete:

- compatibility aliases;
- facade re-exports that exist only for old import paths;
- duplicate helper functions after moving behavior;
- tiny single-purpose helpers whose body is clearer at the call site;
- old tests that assert compatibility paths rather than user-visible behavior;
- dead event conversion helpers;
- old event-log functions once store callers are migrated.

Do not add:

- deprecation layers;
- fallback import paths;
- dual old/new event representations;
- compatibility wrappers around deleted modules.

Prefer inlining over preserving abstraction when a helper:

- is only used in one place;
- simply forwards arguments;
- wraps one expression with no domain name worth preserving;
- exists because code used to live in another module.

Keep a helper only when its name captures a real domain decision, removes
meaningful duplication, or gives tests a useful behavior boundary.

If a public command or external JSON-RPC method depends on behavior, preserve
the behavior but not the internal compatibility path.

## File removals

### Delete `src/zeta/runtime_events.py`

Move its useful behavior into `events.py`.

Before deletion, remove backwards dependencies:

- move `assistant_tool_calls` if it is event-shape logic;
- move `ensure_event_id`;
- move `tool_result_status`;
- move `normalized_tool_result`;
- keep model-provider-specific helpers in `loop.py` only if they are truly
  provider payload helpers.

After deletion, no module should import `zeta.runtime_events`.

### Delete `src/zeta/timeline.py`

Replace it with direct helpers in `events.py` and local filtering in
`session.py`.

The model context read should become something like:

```python
events = reader.list_events(Filter(session_id=session_id, event_type_prefix="zeta."))
model_context = [event_view(event) for event in events if include_in_model_context(event)]
```

If `include_in_model_context` is only used by `session.py`, keep it in
`session.py`. Do not create a new module only for it.

After deletion, no module should import `zeta.timeline`.

## Implementation phases

### Phase 0: behavior baseline

Run the current focused tests:

```sh
uv run pytest tests/test_zeta_event_projection.py tests/test_zeta_agent.py tests/test_zeta_rpc.py -q
uv run pytest tests/test_zeta_model.py -q
```

Identify tests that assert current projected event shapes. Those are the
behavioral guardrails for the refactor.

Also identify compatibility-only tests. Plan to rewrite or delete them once the
new behavior is covered through the direct API.

### Phase 1: add event view helpers to `events.py`

Add:

- `event_view(event: Event) -> dict[str, Any]`;
- `draft_event_view(draft: DraftEvent) -> dict[str, Any]`.

Make existing projection code delegate to these helpers first. Do not delete
`timeline.py` yet.

Verification:

```sh
uv run pytest tests/test_zeta_event_projection.py -q
```

### Phase 2: move runtime draft construction into `events.py`

Move runtime event draft construction from `runtime_events.py` to `events.py`.

Prefer simple functions over typed wrappers unless a wrapper removes real
duplication. The goal is fewer representations, not a better hierarchy.

Update `loop.py`, `session.py`, and `rpc.py` imports to use `events.py`.

Verification:

```sh
uv run pytest tests/test_zeta_agent.py tests/test_zeta_rpc.py -q
```

### Phase 3: make `loop.py` internally event-draft native

Remove raw runtime-event dictionaries from the loop where they are acting as
events.

The loop should record and return `DraftEvent` values. It may still use dicts
for:

- model request messages;
- model response provider payloads;
- capability input/output payloads.

Remove imports from deleted or soon-to-be-deleted event projection code.

Verification:

```sh
uv run pytest tests/test_zeta_agent.py tests/test_zeta_model.py -q
```

### Phase 4: delete `timeline.py`

Move the small remaining useful behavior:

- generic projection to `events.py`;
- model-context filtering to `session.py`;
- trace/object reference decoration to `events.py` only if it is part of the
  canonical event view.

Do not create a replacement timeline module.

Update all imports.

Verification:

```sh
uv run pytest tests/test_zeta_event_projection.py tests/test_zeta_agent.py tests/test_zeta_rpc.py -q
```

### Phase 5: simplify `session.py`

Remove generic event projection helpers from `session.py`.

Session should:

- read events;
- call event view helpers;
- persist drafts;
- publish views;
- return session result dictionaries.

Replace RPC-specific wording such as `"runtime": "zeta-rpc"` with neutral
runtime metadata unless RPC truly owns the value and passes it in.

Verification:

```sh
uv run pytest tests/test_zeta_agent.py tests/test_zeta_rpc.py -q
```

### Phase 6: simplify `rpc.py`

Remove durable event naming and idempotency policy from RPC.

For `events.publish`, RPC should delegate to event/session helpers that produce
`DraftEvent`.

Fix imports that currently use RPC as a facade for session helpers.

Verification:

```sh
uv run pytest tests/test_zeta_rpc.py -q
```

### Phase 7: final cleanup

Remove:

- `src/zeta/runtime_events.py`;
- `src/zeta/timeline.py`;
- legacy event-log helper exports from `zeta.store.events`;
- RPC facade exports for session helpers;
- obsolete `__all__` exports;
- stale tests or test helpers that assert internal conversion paths instead of
  behavior.

Run:

```sh
uv run pytest -q
uv run ty check
uv run ruff check
uvx --with radon radon cc src tests -s
```

Run `pre-commit` only if documentation beyond this plan is updated.

## Stop points

Stop for review after:

1. event view helpers exist and tests still pass;
2. `runtime_events.py` has no remaining imports;
3. `timeline.py` has no remaining imports;
4. `rpc.py` no longer owns durable event naming.

Each stop point should be independently reviewable.
