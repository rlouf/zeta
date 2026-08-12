# zeta-dispatch

`zeta-dispatch` owns the durable runtime boundary below authored Zeta agents.
Its current surface provides:

- deterministic queue-item, attempt, run, publication, and wait identities;
- exhaustive queue-item and attempt state transitions;
- bounded retry policy and stable failure classification;
- declaration-ordered event routing with Python-compatible session templates;
- an exact SQLite implementation of `journal-v0`;
- atomic external ingress, durable routing lifecycle facts, queue and attempt
  projections, fenced claims, exclusion locks, and projection replay;
- atomic attempt completion, cancellation-aware retry decisions, durable
  waits, future event publication, and external-effect safety projections.

The native `zeta` host converts verified authored manifests into these runtime
routes. Dispatch remains independent of the complete authoring model.

The SQLite connection stays private so journal appends and runtime projections
can share one transaction owner. File-backed databases use WAL mode,
`synchronous=FULL`, and a bounded busy timeout. A non-empty database without
the current clean schema epoch is rejected; legacy runtime schemas are not
migrated.

External ingress rejects the complete `runtime.*` namespace. A queueable input
and its unbound `pending` row commit together. Routing records one of three
durable outcomes:

- no matches turn the unbound item into `unhandled`;
- one match binds that same item and records it as `available`;
- several matches close the unbound barrier and create one `available` child
  per route in declaration order.

Session identity and project generation are resolved before work becomes
executable. Lifecycle event IDs and timestamps are explicit inputs; Dispatch
owns every other lifecycle field. Duplicate or colliding generated IDs abort
the complete route transaction.

Queue and attempt rows are read models of the ordered journal. A projection
epoch change rebuilds them instead of migrating rows incrementally. Replay
validates lifecycle payloads and state transitions, preserves incomplete
historical attempts, and discards live ownership state.

Claim tokens are opaque values supplied by the caller and checked together
with the worker, queue item, and exclusive lease deadline. Claiming one item
acquires all of its authored lock keys in the same immediate transaction.
Earlier unbound barriers and earlier unfinished work in the same session block
later claims. Starting an attempt then commits its `queue_item.claimed` and
`attempt.started` facts under one final fencing check. Lease renewal and release
update coordination only; projection rebuilds intentionally clear all claims,
heartbeats, and locks.

Successful completion validates the entire result before appending its first
fact. Immediate publications, future publications, and waits retain their
global result order between `attempt.completed` and `queue_item.completed`.
Cancellation controls use the same ordering and ownership checks, and an
invalid target rolls back the complete success batch.
Matching an external event consumes every compatible active wait and creates
its session continuation atomically. Deadline expiry likewise records the wait
timeout and continuation together. Authorized resource cancellation competes
with wait matching, expiry, and scheduled publication under the same immediate
transaction; whichever terminal fact commits first is retained on retries.
Publishing a due scheduled event also matches waits in the same transaction
before recording the schedule's terminal fact.

Session catalog reads are derived from queue, attempt, and wait projections so
replay cannot disagree with a separate session table. Activity follows the
priority `running → queued → waiting → idle`, exposes cancellation intent, and
rejects histories that assign one session to several agents. Direct user turns
cancel an active owned wait, append `session.message.requested`, and bind the
new queue item in one transaction; keyed retries cannot cancel a later wait.
Public run cancellation resolves back to the stable queue item and reuses the
same intent-first cancellation lifecycle.

External-effect facts form the strict lifecycle `planned → started →
completed|failed|ambiguous`. Their retry-stable keys use the same canonical JSON
and domain-separated hashing as the Rust agent. A started or ambiguous
`unsafe_to_retry` effect remains discoverable after projection rebuild so a
later attempt can dead-letter the item without repeating the external call.

Recurring cron and catch-up evaluation remain an authoring/runner concern.
Dispatch accepts one already-resolved occurrence plus an optional explicit
activation fact, then atomically records that batch with the external scheduled
event and scheduler decision. Occurrence keys make repeated ticks safe and the
decision projection makes rebuild exact.

`ingest_event` is the external authority boundary. `append_event` is the
lower-level trusted journal adapter used for runtime-owned lifecycle facts and
journal conformance; application ingress should not call it directly.

## Example

```rust
use serde_json::Map;
use zeta_dispatch::Dispatch;
use zeta_journal::{Event, HeadExpectation};

let mut dispatch = Dispatch::open_in_memory()?;
let event = Event {
    id: "evt_example".to_owned(),
    event_type: "example.created".to_owned(),
    source: "example".to_owned(),
    payload: Map::new(),
    idempotency_key: Some("example:1".to_owned()),
    caused_by: None,
    session_id: None,
    run_id: None,
    turn_id: None,
    timestamp_ms: 1,
    cursor: None,
};

let appended = dispatch.append_event(event.clone())?;
assert!(appended.inserted);
assert_eq!(appended.event.cursor, Some(1));

let duplicate = dispatch.append_event(event)?;
assert!(!duplicate.inserted);
assert_eq!(duplicate.event.cursor, Some(1));
assert_eq!(
    dispatch
        .verify_journal(HeadExpectation::Unanchored)?
        .entries_checked,
    1,
);

# Ok::<(), zeta_dispatch::DispatchError>(())
```

Portable behavior is pinned by
[`spec/vectors/dispatch/runtime.json`](../../spec/vectors/dispatch/runtime.json)
and the shared journal vectors under
[`spec/vectors/journal`](../../spec/vectors/journal).
