# Runtime Semantics

Zeta separates durable historical facts from live coordination state even
though both are stored in one SQLite database.

## Runtime Journal

The `events` table is the durable journal. Ingress, returned events, queue
lifecycle events, attempt lifecycle events, and connector delivery events are
historical facts. They retain their event ids, idempotency keys, causality, and
append order.

The following tables are projections of the journal and may be deleted and
rebuilt:

- `queue_items`
- `attempts`
- `attempt_results`
- `session_mappings`

Rebuilding them must produce the same terminal queue and attempt history.

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
