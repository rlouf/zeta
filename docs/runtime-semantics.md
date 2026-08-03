# Runtime Semantics

Zeta separates durable historical facts from live coordination state even
though both are stored in one SQLite database. The harness owns this document's
subject. For where each layer lives in the source tree, see
[Where Things Live](concepts.md#where-things-live).

The runtime store exposes these as separate `journal` and `coordination`
interfaces. They intentionally share a connection and write lock today; the
separation defines ownership and recovery semantics rather than requiring a
second database.

`QueueingDispatcher` is the production execution path for both `zeta run` and
`zeta serve`. It claims work, acquires locks, and delegates attempts to the same
`AttemptCoordinator`. Tests and embedded callers can use the identical path
with `RuntimeEventStore.open(":memory:")`.

## Harness And Agent Loop

The harness owns event ingress, scheduling, queue claims, locks, retries,
lifecycle records, and external effects. An agent loop owns only one
invocation. `AgentLoop` is the callable that marks this boundary:

```python
AgentLoop = Callable[
    [AgentInvocation, str, list[dict[str, Any]], str, AgentConfig, str, str],
    Awaitable[AgentRunResult],
]
```

The harness supplies the invocation, the rendered objective, the timeline, the
project context, the model configuration, the session id, and the run id. The
loop returns an `AgentRunResult`. `AgentInvocation` carries the matched
definition, the triggering event, and the queue, attempt, and run ids.

An agent loop receives no runtime database, no queue state, and no retry
authority. `compile_agent_definition` accepts an `agent_loop` argument, so an
embedded caller can run the loop in another environment. The local harness
stays the source of truth. The bundled `RuntimeAgentLoop` is the in-process
default; the worker builds it and passes its `run` method. Remote transport,
tool brokering, and trace streaming build on this boundary. They are not
separate runtimes.

### Agent Event Requests

An authored agent that declares `publishes` receives the `publish_event` control
tool. The tool accepts one declared event type, its payload, and an optional
absolute `at` time. The time must include a UTC offset.

The agent must use `publish_event` to request each event. Zeta does not infer
events from the model response.

The loop validates the request and returns a stable handle. The loop does not
write the requested event. The loop adds an ordered `PublishEventRequest` to the run
result. This keeps event writes under the queue claim that owns the attempt.

The coordinator performs the final claim check inside one journal transaction. It
sends event notifications only after that transaction commits. The transaction
contains:

```text
runtime.attempt.completed
requested events or runtime.scheduled_event.created facts
runtime.queue_item.completed
```

A failed, cancelled, or stale attempt writes none of its requests. A retry
uses the source queue item and request position to get the same handle and
idempotency key.

## Runtime Journal

The `events` table is the durable journal. Ingress, published events, queue
lifecycle events, attempt lifecycle events, and connector delivery events are
historical facts. They retain their event ids, idempotency keys, causality, and
append order.

The following tables are projections of the journal and may be deleted and
rebuilt:

- `queue_items`
- `attempts`
- `attempt_results`
- `scheduled_events`
- `waits`
- `session_mappings`

Rebuilding them must produce the same terminal queue and attempt history.

## Derived Identity

Only an event gets a random id. Every id below an event is a pure function of
it:

```text
event_id       = evt_<uuid>
queue_item_id  = qi_<event_id>_<agent_id>
attempt_id     = att_<queue_item_id>_<attempt_number>
run_id         = run_<attempt_id>
publish_handle = pub_<hash(queue_item_id, position)>
wait_handle    = wait_<hash(queue_item_id, position)>
```

This is what makes the chain idempotent. A router that sees the same event
twice computes the same `queue_item_id`, so the second routing attempt
collides with the first instead of creating parallel work. A unique index on
`events.idempotency_key` enforces the same rule at ingress.

Lifecycle events carry derived idempotency keys for the same reason. An
attempt lifecycle key is `attempt:<queue_item_id>:<attempt_number>:<status>`,
so a retried append of a status that was already recorded is a duplicate, not
a new fact.

Session ids follow the agent's dispatch mode. A `session_scoped` agent uses
`agent/<agent_id>`, and a `one_shot` agent uses `agent/<agent_id>/<event_id>`.
Attempt numbers, not session ids, distinguish retries.

## Coordination State

Claims, claim tokens, lease deadlines, attempt heartbeat updates, and locks are
live coordination state. They fence concurrent workers but are not historical
facts and are not reconstructed after a projection rebuild.

A rebuild applies these recovery rules:

1. All locks are discarded.
2. Claim owners, tokens, and deadlines are discarded.
3. A non-terminal queue item whose last lifecycle state was `claimed` becomes
   `available`, or `pending` when it has not yet been bound to an agent.
4. An attempt without a terminal lifecycle event remains visible as an
   incomplete historical attempt. A new claim creates the next attempt number.

Normal worker startup also treats a claimed queue item without a lease as
expired. This handles interrupted rebuilds and databases produced by older
runtime versions.

## Queue Item State Machine

Normal work follows one of these paths:

```text
pending -> available -> claimed -> completed
                            |  -> cancelled
                            |  -> dead_lettered
                            `  -> available  (retry)

pending -> unhandled
available -> cancelled
```

`pending` is the unbound queue state created for an ingress event. Routing may
bind it directly when one agent matches, fan it out into multiple `available`
items, or mark it `unhandled`.

Terminal queue states are `completed`, `cancelled`, `dead_lettered`, and
`unhandled`. No later lifecycle transition is legal for a terminal item.

## Attempt State Machine

Each claim may create one numbered attempt:

```text
running -> completed
        -> failed
        -> cancelled
```

Retries create a new attempt; they never restart or mutate a terminal attempt.

An agent-published event is appended and projected as pending work during the
successful completion transaction. The attempt reaches a terminal state before
the event becomes downstream work. No recursive dispatch path remains: an
agent never runs a downstream event inside its own attempt.

## One-Shot Scheduled Events

A future `publish_event` request creates a
`runtime.scheduled_event.created` fact. The `scheduled_events` projection keeps
its target time and terminal state. A rebuild restores this state from created,
published, and cancelled facts.

Before a worker claims queue work, it publishes one due scheduled event. It
uses one SQLite transaction to claim the row, append the normal event, append
`runtime.scheduled_event.published`, and update the projections. If a crash
occurs, SQLite rolls back the transaction. The scheduled event stays pending.
Two workers cannot publish the same row.

Cancellation appends `runtime.scheduled_event.cancelled`. Publication and
cancellation use the same conditional claim, so only one can win.

Inspect and cancel these requests with:

```sh
zeta events scheduled
zeta events scheduled --json
zeta events cancel-scheduled <handle>
```

## Durable Agent Waits

An authored agent can end a successful run with `wait_for`. The loop returns a
`WaitRequest`; it does not write runtime state. Inside the attempt completion
transaction, the coordinator appends `runtime.wait.created` between
`runtime.attempt.completed` and `runtime.queue_item.completed`.

The `waits` projection stores the active condition, exact top-level payload
fields, optional absolute deadline, source session, agent, queue item, and
project generation. One session may own one active wait. A second request is a
permanent attempt failure and leaves the existing wait unchanged.

Inspect active and terminal waits with:

```sh
zeta waits list
zeta waits list --json
```

A newly stored non-runtime event checks active waits in the same transaction.
If its type and selected top-level payload fields match, Zeta appends
`runtime.wait.matched` and a directly targeted queue item. The original event
still enters normal routing. Duplicate delivery cannot consume the wait twice.

Before a worker claims queue work, it expires one due deadline. Expiry appends
`runtime.wait.timed_out` and a directly targeted queue item in one transaction.
Matching and expiry both require an active row, so only one can win.

The continuation uses the stored agent, session, and project generation. A
matched continuation renders the agent prompt with the original event type and
payload. A projection rebuild restores the terminal wait and continuation from
the lifecycle facts.

## Claim Fencing

Every durable claim has an opaque token. Heartbeats and claim releases must
match the worker and token. The dispatcher checks ownership before execution,
after exceptions, and before terminal lifecycle events. A stale worker may not
commit terminal Zeta state.

Claim fencing does not undo an external side effect that already happened.
Connector and capability effect semantics define whether such an operation can
be retried, deduplicated, or must stop for manual resolution.

## External Effects

Side-effecting connector and capability operations are journaled as:

```text
planned -> started -> completed
                   -> failed
                   -> ambiguous
```

An effect key identifies the logical operation independently of the numbered
attempt. Connector effects use the connector/event idempotency key. Capability
effects hash the queue-item scope, capability id, and canonical arguments.

`idempotent_with_key`, `connector_deduplicated`, and `at_least_once` operations
may enter the normal retry policy. `at_least_once` explicitly permits duplicate
delivery. `unsafe_to_retry` does not: if its last durable state is `started` or
`ambiguous`, a later attempt records a permanent failure and dead-letters the
queue item without invoking the agent again.

Zeta does not claim exactly-once external delivery. The journal proves what the
runtime planned and observed; the declared semantics state what a provider can
guarantee.

## Runtime Health Measurements

`RuntimeEventStore` accepts a backend-neutral metrics sink and uses a no-op sink
by default. The coordinated runtime records:

- `sqlite.event_append_ms`, `sqlite.queue_claim_ms`,
  `sqlite.lock_acquire_ms`, `sqlite.heartbeat_write_ms`, and
  `sqlite.trace_close_ms` for synchronous storage work;
- `runtime.queue_lag_ms`, `runtime.heartbeat_delay_ms`, and
  `runtime.event_loop_delay_ms` for scheduling health;
- `runtime.agent_execution_ms`, `runtime.retries_scheduled`, and
  `runtime.lock_conflicts` for execution pressure.

Metrics delivery is best effort. A failing sink cannot fail or roll back a
runtime transition.
