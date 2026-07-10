# Runtime Hardening Implementation Plan

## Objective

Make Zeta's execution semantics easier to prove while preserving its existing
event, CLI, and SQLite contracts. The target runtime has one canonical queued
execution path, immutable project generations, explicit external-effect
guarantees, and a documented boundary between historical truth and live
coordination.

## Current State

- Connector egress already carries idempotency keys, and the Slack connector
  forwards them as `client_msg_id`. Delivery semantics and retry enforcement
  are not yet declared.
- Unknown manifest sections are rejected during project validation. The
  runtime `locks` field is still implemented as an undocumented extension.
- Queue and attempt projections can be rebuilt from lifecycle events, but the
  recovery contract for active claims and locks is not explicit.
- `EventDispatcher` and `QueueingDispatcher` separate immediate and durable
  execution, but they still expose different guarantees.

## Milestone 1: Define The Runtime Contract

1. Document legal queue and attempt transitions, lease expiry, stale
   completion, retry, dead-letter, cancellation, and crash behavior.
2. Add fault-injection tests for claim loss, heartbeat failure, duplicate
   workers, and crashes around lifecycle writes.
3. Version runtime projections and define rebuild behavior. Rebuilds discard
   active coordination ownership and make unfinished work claimable again.

Exit condition: every transition and recovery case has one documented expected
outcome and an automated test.

## Milestone 2: Compile And Capture Runtime Definitions

1. Introduce a `ProjectSnapshot` loaded and validated once per worker
   generation.
2. Compute a deterministic generation id from agent sources, event schemas,
   skills, connectors, capabilities, model resolution, and runtime version.
3. Store snapshot manifests as immutable content-addressed records.
4. Bind routed queue items and attempts to a project generation.
5. Record an execution manifest containing the exact agent, authority, model,
   schema, connector, and runtime definition used by each attempt.

Exit condition: existing queued work does not silently change definition when
the project files change.

## Milestone 3: Extract State-Machine Ownership

1. Extract lifecycle recording and deterministic event routing from the
   dispatcher.
2. Extract an `AttemptCoordinator` that owns attempt start, ownership checks,
   heartbeat lifetime, success, failure, retry, and dead-letter decisions.
3. Separate agent execution from validation and publication of returned
   events.
4. Keep the current dispatcher API as a compatibility facade while callers
   migrate.

Exit condition: `dispatch.py` composes the runtime but does not own the attempt
state machine.

## Milestone 4: Define External Effect Guarantees

1. Add connector and capability delivery semantics:
   `idempotent_with_key`, `connector_deduplicated`, `at_least_once`, and
   `unsafe_to_retry`.
2. Record durable planned, started, completed, failed, and ambiguous effects.
3. Derive attempt-independent effect keys and propagate the same key on every
   permitted retry.
4. Prevent automatic retries after ambiguous unsafe effects.
5. Make connector egress failures follow declared semantics instead of always
   completing their queue item.

Exit condition: crash tests demonstrate the delivery behavior of every effect
class.

## Milestone 5: Fix Queue Projection And Unify Execution

1. Project pending work transactionally when queueable events are appended and
   remove full-history queue discovery.
2. Separate the `RuntimeJournal` API from the `CoordinationStore` API while
   retaining SQLite as the storage model.
3. Introduce one runtime coordinator for tests, `zeta run`, and `zeta serve`.
4. Provide an in-memory coordination implementation for lightweight use.
5. Deprecate immediate recursive execution and complete the current attempt
   before executing downstream returned events.

Exit condition: production and test execution use the same transitions and
guarantees.

## Milestone 6: Operations And Authoring Cleanup

1. Instrument SQLite transaction latency, lock wait, queue lag, heartbeat
   delay, trace writes, retries, and event-loop stalls.
2. Add property tests for transition legality, projection rebuild equivalence,
   duplicate claims, and effect crash points.
3. Make `locks` a typed authored-agent field.
4. Reserve `x-*` for custom frontmatter and reject other unknown unnamespaced
   fields.

## Deferred

- Renaming the `zeta` and `zetad` packages.
- Replacing synchronous SQLite access or changing database backends.
- Adding orchestration expressions or branching to agent frontmatter.

These do not improve the runtime's correctness enough to precede the state,
snapshot, effect, and queue work above.
