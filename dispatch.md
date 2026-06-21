# Event-Sourced Dispatch

## Current Model

The runtime has one trigger path:

```text
DraftEvent
  -> Event
  -> matching agents
  -> QueueItem per matched agent
  -> Attempt for the queue item
  -> loop execution
  -> terminal Attempt event
  -> terminal QueueItem event
```

Interactive `session.run` is not a separate runner. It appends
`session.turn.requested`, routes that event to the built-in
`zeta.session.turn` agent, and observes the terminal queue item for that agent.

Event-triggered agents use the same path with their own accepted event types.

## Ownership

`src/zeta/dispatch.py` owns:

- appending accepted events
- publishing live events
- matching events to registered agents
- constructing `QueueItem` and `Attempt` kernel objects
- serializing queue item and attempt lifecycle events
- exposing terminal queue item result selection for RPC observation

`src/zeta/session.py` owns:

- session request parsing
- adapting `session.turn.requested` into the loop call
- recording user/runtime/model/tool events for the turn
- assembling the session result from persisted events

`src/zeta/loop.py` owns model/tool execution. It should not know whether the
turn came from JSON-RPC, a webhook, or an agent-published event.

`src/zeta/rpc.py` owns JSON-RPC protocol shape. For `session.run`, it appends a
`session.turn.requested` event and maps the terminal queue item lifecycle event
back to the RPC response.

## Durable Lifecycle

Queue item payloads are the serialized `QueueItem` shape:

```text
queue_item_id
event_id
target_agent
status
```

Attempt payloads are the serialized `Attempt` shape:

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

Lifecycle events are runtime-owned. External ingress cannot publish
`runtime.queue_item.*` or `runtime.attempt.*` events.

## Simplification Checklist

- [x] Add kernel `QueueItem` and `Attempt` shapes.
- [x] Replace vague work events with queue item and attempt lifecycle events.
- [x] Rename dispatch output to `lifecycle_events`.
- [x] Remove direct agent results from `DispatchOutcome`.
- [x] Split append/publish from route/execute with `publish_event` and `route`.
- [x] Make `session.run` append `session.turn.requested` and observe the
  terminal queue item for `zeta.session.turn`.
- [x] Register the interactive runner as a normal built-in agent.
- [x] Keep `loop.py` as the model/tool execution engine.
- [x] Add agent-published events through `AgentInvocation.publish(...)`.
- [x] Remove separate queue item and attempt projection objects.
- [x] Remove direct `AgentTurnResult` trace fallback from `session.py`.
- [x] Remove old terminal-agent result selection; use terminal queue item result
  selection by source event and target agent.
- [x] Remove speculative dispatch-hop and self-publication policy from v0.
- [x] Delete tests that only pinned removed compatibility behavior.
- [x] Collapse this document from migration log to current model.

## Remaining TODOs

- [ ] Keep cleaning `session.py` when a helper becomes single-use or its name no
  longer clarifies the loop sink.
- [ ] Add retry scheduling only when there is a real worker retry path.
- [ ] Add queue claiming/lease fields only when there are multiple workers.
- [ ] Reintroduce publication-loop protection only as an explicit scheduling
  policy if agent-authored loops become a real problem.
- [ ] Clean up any obsolete code in the same slice that makes it unnecessary.
