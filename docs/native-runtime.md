# Native Runtime Design

## Current implementation

The first native slice now has one dispatch actor. It owns the SQLite
connection and routes native `events.publish` calls as soon as they commit.
`RuntimeWake` records each successful ingress or recovery transition.

The actor recovers unbound ingress, expired claims, due waits, and due deferred
publications at start. It waits for the next durable deadline between commands.
`zeta status` reports queue state and the next deadline through `runtime.status`.

The agent lane now claims up to four independent queue items. It runs each
agent outside the Dispatch actor, renews its lease, and commits its terminal
proposal before it wakes more work. Queue items retain their archived agent
revision across reload and restart.

The native executor supports model profile references, durable controls, and
selected native tools. `zeta.web_search` posts to Codex `alpha/search`.
The runner saves normal tool call records. It sends text and bounded source
records back to the model. The egress lane stores connector effects, claims
them separately from agent work, and retries safe delivery. The native command
does not start connector processes yet. Connector process support remains
future work.

## Model profiles

An agent can name a model profile in its frontmatter.

```yaml
model: fast-local
```

Define project profiles in `zeta.toml`.

```toml
[[models]]
name = "fast-local"
model = "qwen3.5"
url = "http://127.0.0.1:8080/v1/chat/completions"
default = true
```

`zeta up` resolves a project profile before a local profile.
It uses `~/.zeta/models.toml` after a project miss.
It warns when an explicit agent profile uses the local file.
An agent without `model` uses the project default, then the local default.
It emits no warning for either default.
It sends one bounded request to each resolved model before readiness.
It stops before startup when no required profile exists.

## Decision

The native host will use an event-driven dispatcher. It will not poll one queue item per
pass. SQLite remains the durable authority. A local wake signal only reduces
the time from commit to work.

The complete host will have three parts:

| Part | Owns | Does not own |
| --- | --- | --- |
| Dispatch actor | The `Dispatch` connection and durable transitions | Model and connector calls |
| Runtime driver | Wakeup, timers, capacity, task results, and shutdown | SQLite transactions |
| Workers | One claimed agent attempt or egress delivery | Claims and final durable state |

`Dispatch` has one synchronous SQLite connection. A dedicated blocking actor
must own it. Tokio tasks send commands to that actor. This avoids shared
connections and preserves one transaction owner.

## Work flow

```text
connector, scheduler, or agent completion
  -> Dispatch actor commits facts, routes, and delivery intents
  -> actor increments the wake epoch after commit
  -> runtime driver claims work until each lane reaches capacity
  -> agent and delivery tasks execute outside SQLite
  -> task result returns to the actor for one durable completion
```

An agent completion commits its lifecycle facts, published events, route
decisions, and egress intents before the actor sends a wakeup. A crash cannot
leave a committed event without durable route or delivery state.

## Wakeup and timers

`RuntimeWake` will own a Tokio `watch<u64>` epoch. Every successful command that
may create eligible work increments the epoch after its SQLite transaction
commits. Receivers can coalesce many increments. They must never infer lost
work from coalescing.

The driver will follow this sequence:

1. Claim work until each lane reaches its capacity.
2. Ask the actor for the earliest durable deadline.
3. Record the current wake epoch.
4. Ask the actor whether eligible work exists.
5. Claim again if work exists.
6. Otherwise await a changed epoch, the deadline, or shutdown.

The second eligibility check closes the race between the first drain and the
wait. An epoch change before the wait remains observable. An epoch change after
the check wakes the wait.

The actor will return the earliest of these deadlines:

- the next schedule occurrence from the active revision;
- the next deferred publication;
- the next wait timeout;
- the next retry availability time;
- the next claim or lock expiry.

On a deadline, the actor runs one bounded `advance_due(now)` command. It emits
all due schedule events and advances every due deferred publication or wait.
It then increments the wake epoch only if the command commits new work.

The driver runs `recover(now)` and an initial drain on start. Wake signals are
therefore an optimization, not a recovery mechanism.

## Queue lanes

The runtime has separate bounded lanes.

| Lane | Unit | Default rule |
| --- | --- | --- |
| Agent | One claimed queue item | Run up to four independent items |
| Egress | One claimed outbox delivery | Do not wait for available agent capacity |
| Control | Reload, cancel, and shutdown commands | Always accept; no external call |

`agent_capacity` and `egress_capacity` are separate settings. The native agent
lane currently uses four slots. The design must support a later configuration
for both values.

The agent lane will use the existing `queue_items`, `queue_claims`, locks, and
attempt lifecycle. The runtime must renew a live claim before its deadline.
The renewal stops when the task returns or loses authority.

The egress lane uses the durable connector-effect outbox. It must not use a fake
agent id or share the agent lane. Slow or failed connector delivery must not delay
an unrelated model call.

## Durable outbox

`zeta-dispatch` stores connector deliveries in its `effects` projection and
matching `effect_claims` rows. Each delivery has these fields:

- a deterministic delivery id from the source event and egress binding;
- the source event id and project revision;
- the connector id, operation, payload, and binding options;
- the idempotency key and declared delivery semantics;
- availability, attempt, claim, and terminal state;
- an optional connector ordering key.

The actor scans committed agent-published events before it drains egress. It
stores a delivery before it calls a connector. A restart repeats that scan.
A unique source-event and binding key returns the original delivery.

`effects` is the durable record for external-effect semantics. The outbox is
its delivery projection. A delivery claim appends `runtime.effect.started`
before the connector call. A terminal result appends `runtime.effect.completed`,
`runtime.effect.failed`, or `runtime.effect.ambiguous`.

The first release has no cross-delivery order guarantee. A connector can later
declare an ordering key. The claim query then admits only the earliest active
delivery for that key.

## Egress delivery

The runtime will call a connector only after the actor commits the planned effect.
The connector call runs outside the SQLite transaction.

The result rules are:

| Semantics | After a lost worker or uncertain connector result |
| --- | --- |
| `idempotent_with_key` | Retry with the same idempotency key |
| `connector_deduplicated` | Retry with the same idempotency key |
| `at_least_once` | Retry and record possible duplicate delivery |
| `unsafe_to_retry` | Mark ambiguous and require operator action |

The actor will apply the same rules during recovery. It will never repeat an unsafe
delivery automatically.

## Dispatch actor commands

The actor API will use typed commands. Each command will return the commit
outcome and whether it created eligible work.

```text
Ingress(event, route_plan)
AdvanceDue(now)
ClaimAgent(worker, token, lease, now)
StartAttempt(claim, identities, now)
CompleteAttempt(claim, completion, route_plan, now)
RenewClaim(claim, now)
ClaimDelivery(worker, token, lease, now)
CompleteDelivery(claim, result, now)
Recover(now)
NextDeadline(now)
Status()
```

`CompleteAttempt` accepts the immutable revision from the claimed queue item.
It never rereads authored files. `Ingress` and `CompleteAttempt` receive a
compiled route plan from the active revision. They store the revision with
each queued item and outbox delivery.

The initial implementation can keep the existing claim then start sequence.
It should later add `claim_next_attempt` as one dispatcher API. The API can
perform both durable transitions before it returns execution input.

## Reload and status

`zeta reload` validates and atomically stores the next project revision. It
then sends a wakeup so the timer uses the new schedule plan. Existing queued
items and outbox deliveries retain their stored revision.

`zeta status` reports separate agent and egress lane counts. It will also report
the next deadline and the oldest available work time. This makes delay visible
without log inspection.

## Recovery and shutdown

On start, the actor will reconcile expired claims and locks. It will retain historical
attempt facts and creates a later attempt only through the normal claim path.

For egress, `planned` work will become available. A `started` delivery will follow its
declared semantics. The runtime will not silently convert an unsafe delivery
to a retry.

On shutdown, the driver will stop new claims. It will ask agent tasks to stop
and wait for their durable result or lease expiry. It will ask connector tasks
to stop. The actor will write no synthetic successful result.

## Remaining implementation order

1. Add durable native-tool effect handling to the agent executor.
2. Add schedules and connector process supervision after durable egress works.

## Acceptance tests

- An ingress commit wakes an idle runtime without a fixed poll delay.
- A commit during driver standby cannot leave work unclaimed.
- A due schedule fires at its deadline without periodic scanning.
- A completed agent event creates one durable egress delivery before a call.
- Egress starts while the agent lane is full.
- A restart recovers every eligible queue item and delivery.
- Every retry uses the original effect idempotency key.
- An unsafe uncertain delivery becomes ambiguous and never retries itself.
- Reload changes future route and schedule plans only.
