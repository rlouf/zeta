# Event-Sourced Dispatch Implementation Plan

## Target Shape

Refactor the Python dispatcher toward the `zeta-dispatch` model, but use the
clearer queue item / attempt ontology:

```text
validate
  -> enrich/store Event
  -> publish Event
  -> route Event
  -> create QueueItem per matching agent
  -> create Attempt when a worker claims a QueueItem
  -> run loop
  -> finish Attempt
  -> complete, retry, fail, or cancel QueueItem
```

The goal is one event-sourced run path for interactive `session.run` and
event-triggered agents.

## Behavior To Preserve

- `events.publish` appends incoming events and publishes them.
- Duplicate idempotency keys do not route queue items twice.
- Interactive `session.run` validates params and returns the final answer.
- Runtime UI events can still be streamed live.
- Cancellation and deadlines produce terminal run results.
- Agent failures produce failed attempts rather than crashing the dispatcher.
- Existing model/tool loop behavior stays inside `loop.py`.

## Current Pain

- `session.run` creates `session.turn.requested`, routes it through a generic
  dispatcher, and then unwraps `agent_results[0]`.
- `DispatchOutcome` mixes append results, lifecycle events, and agent return
  values.
- The legacy `runtime.work.*` model was vague: it conflated queue item state,
  attempt state, and final agent results.
- Interactive runs and event-triggered runs are conceptually separate even
  though they need the same lifecycle.

## Intended Design Improvement

Make appended events the only trigger surface.

Make queue items the durable association between events and agents.

Make attempts the durable record of one worker trying to process one queue item.

Make synchronous interactive RPC a client-side observation mode over a queue
item or attempt, not a special execution path.

## Step 1: Lock Existing Behavior

Add or update focused pytest coverage before changing implementation:

- dispatching an unmatched event appends the event and records an unhandled
  queue item outcome
- dispatching a matched event creates a queue item, starts an attempt, and
  completes both on success
- duplicate idempotency keys append once and do not create a second queue item
- a failing agent records a failed attempt and preserves the error
- `session.run` still returns the final answer
- `session.run` duplicate `run_id` does not execute twice
- cancellation maps to cancelled attempt and queue item state
- live stream events still reach `publish_event`

Run targeted tests after this step:

```bash
uv run pytest tests/test_zeta_agent.py tests/test_zeta_agents.py
```

## Step 2: Add Kernel Shapes

Add pure shared shapes under `src/zeta/kernel/`.

Suggested module:

```text
src/zeta/kernel/dispatch.py
```

Suggested shapes:

```python
QueueItemStatus = Literal[
    "available",
    "claimed",
    "completed",
    "failed",
    "cancelled",
    "retry_scheduled",
    "unhandled",
]

AttemptStatus = Literal[
    "running",
    "completed",
    "failed",
    "cancelled",
]
```

And frozen dataclasses for:

```text
QueueItem
Attempt
```

Keep these shapes boring and serializable. Do not put store access, claim logic,
retry scheduling, or bus behavior in kernel.

## Step 3: Introduce Queue Item And Attempt Event Helpers

Create a narrow lifecycle event surface in the dispatch layer.

The helper should build drafts for queue item events:

```text
runtime.queue_item.created
runtime.queue_item.claimed
runtime.queue_item.completed
runtime.queue_item.failed
runtime.queue_item.cancelled
runtime.queue_item.retry_scheduled
runtime.queue_item.unhandled
```

And attempt events:

```text
runtime.attempt.started
runtime.attempt.heartbeat
runtime.attempt.completed
runtime.attempt.failed
runtime.attempt.cancelled
```

Queue item payloads should include:

```text
queue_item_id
event_id
target_agent
status
```

Attempt payloads should include:

```text
attempt_id
queue_item_id
event_id
attempt_number
target_agent
status
started_at
finished_at
error
session_id
```

Keep environment-specific metadata optional:

```text
agent_sha256
base_commit_hash
commit_hash
merge_error
branch_name
worktree_path
```

Use stable idempotency keys:

```text
queue_item:<event_id>:<target_agent>:created
queue_item:<event_id>:<target_agent>:claimed:<attempt_number>
queue_item:<event_id>:<target_agent>:completed
queue_item:<event_id>:<target_agent>:failed
queue_item:<event_id>:<target_agent>:cancelled
queue_item:<event_id>:unhandled
attempt:<queue_item_id>:<attempt_number>:started
attempt:<queue_item_id>:<attempt_number>:completed
attempt:<queue_item_id>:<attempt_number>:failed
attempt:<queue_item_id>:<attempt_number>:cancelled
```

Keep this local and explicit. Do not add a generic framework around it.

## Step 4: Separate Append/Publish From Route/Execute

Change the dispatcher internals into two operations:

```python
publish_event(draft: DraftEvent, *, route: bool = True) -> AppendOutcome
route(event: Event) -> None
```

`publish_event` should:

- reject external ingress for reserved `runtime.queue_item.*` and
  `runtime.attempt.*` events
- append to the event store
- publish the durable event only when inserted
- route only when inserted and `route=True`

`route` should:

- find matching agents
- record an unhandled queue item outcome when none match
- create one queue item per matching agent
- claim each queue item by starting an attempt
- execute matching attempts concurrently where current behavior requires it
- emit terminal attempt and queue item events after execution

At the end of this step, `DispatchOutcome.agent_results` should no longer be the
primary state carrier. The durable event log should be.

## Step 5: Add Queue Item And Attempt Projections

Add small projections that derive current queue items and attempts from runtime
lifecycle events.

The projections should be rebuildable from the event store and updated as new
runtime lifecycle events are appended.

Minimal projected queue item fields:

```text
queue_item_id
event_id
target_agent
status
last_event_id
```

Minimal projected attempt fields:

```text
attempt_id
queue_item_id
event_id
attempt_number
target_agent
status
started_at
finished_at
error
session_id
last_event_id
```

These projections are for observation and waiting. They are not authoritative
state.

## Step 6: Make Interactive Turns A Built-In Agent

Represent interactive execution as an agent accepting:

```text
session.turn.requested
```

Move the current `session_event_dispatcher` idea into dispatcher registration,
not a special helper that builds a one-agent dispatcher per run.

The interactive runner should call the existing session turn logic:

```text
session.turn.requested event
  -> QueueItem(target_agent=zeta.interactive)
  -> Attempt
  -> SessionRunParams
  -> async_run_agent_turn
  -> Turn result
  -> terminal attempt + queue item events
```

Keep `loop.py` mostly unchanged. The loop should continue to emit draft events
through an event sink supplied by the runner.

## Step 7: Change `session.run` To Append And Observe

Change `run_rpc_session` so it:

1. validates params
2. chooses or reads `run_id`
3. appends `session.turn.requested`
4. waits for the queue item / attempt for `(event_id, zeta.interactive)` when
   synchronous
5. maps the terminal attempt / turn projection to the existing JSON-RPC result
   shape

This removes the need to unwrap `outcome.agent_results[0]`.

The request/response API can remain stable while the internal source of truth
moves to events.

## Step 8: Agent-Published Events

Add one path for agents to publish follow-up events into the same dispatcher.

The dispatcher should attach active event context:

```text
caused_by = active_event.id
session_id = active_event.session_id unless explicitly supplied
turn_id = active_event.turn_id when present
queue_item_id = active_queue_item.queue_item_id
attempt_id = active_attempt.id
```

Add a conservative hop limit to prevent self-recursive event storms.

Reject direct self-publication of the same event type from the active event
unless a concrete use case appears.

## Step 9: Clean Up Old Return-Oriented APIs

Once tests pass through the event-sourced path:

- remove `run_session_turn_from_event` if it is only adapter glue
- remove per-run construction of `session_event_dispatcher`
- reduce or remove `DispatchOutcome.work_events`
- reduce or remove `DispatchOutcome.agent_results`
- remove any remaining compatibility references to legacy work-event names
- keep compatibility aliases only where external callers still need them

Do not preserve backward compatibility for internal APIs unless a caller still
uses them during the refactor.

## Step 10: Verification

Run targeted tests first:

```bash
uv run pytest tests/test_zeta_agent.py tests/test_zeta_agents.py tests/test_zeta_event_projection.py
```

Then run the full suite:

```bash
uv run pytest
```

Run complexity checks after non-trivial Python edits:

```bash
uvx --with radon radon cc src tests -s
```

If documentation files are updated during implementation, run:

```bash
uv run pre-commit run --all
```

## Open Decisions

- Should stream chunks be durable events or live-only notifications?
- Should unmatched events create a `runtime.queue_item.unhandled` event for
  every event, or only for events that enter a routable namespace?
- Should synchronous `session.run` wait by polling the store, subscribing to a
  bus, or awaiting the dispatch task directly while still deriving the result
  from terminal events?
- Should queue items and attempts first be event-sourced projections only, or
  should there be materialized tables later for efficient worker claiming?
