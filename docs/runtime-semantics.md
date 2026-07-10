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
