# EventConnector Plan

## Goal

Add a small connector interface for external systems that can move events into and out of Zeta.

The scope is deliberately narrower than a general extension system:

- Ingress turns external facts into durable Zeta events.
- Agents consume durable events and publish durable events.
- Egress subscribes to durable Zeta events and performs external side effects.

Tools, skills, resources, and model providers may also become extension points later, but this design should not imply ownership of those surfaces.

## Existing Shape

The Python runtime already has most of the required event machinery:

- Agent manifests can declare `ingress` and `egress` sections.
- EventConnector-provided event schemas are merged into the project event registry.
- Ingress handlers can produce `DraftEvent`s from either a poll tick or an external request that is accepted into the durable event log.
- Egress handlers are wired as durable queue executors for matching event types.
- Egress lifecycle is recorded as `runtime.egress.started`, `runtime.egress.completed`, or `runtime.egress.failed`.

The missing piece is a public connector/discovery/configuration boundary. Today the connector resolver is mostly a test/runtime injection point.

## Event Model

Ingress and egress should be represented as event ownership and event subscription, not as named sources or sinks in every binding.

Recommended naming convention:

- Ingress facts use past tense:
  - `slack.message.received`
  - `slack.mention.received`
- Egress intents use direct action names:
  - `slack.message.post`
  - `slack.message.reply`
  - `slack.message.update`
  - `slack.message.delete`

Avoid names like `slack.message.send.requested`. The event is already a durable fact in the Zeta log, and egress lifecycle events record whether the side effect started, completed, or failed.

## Manifest Shape

The agent manifest should reference event types directly.

```yaml
accepts:
  - slack.message.received
returns:
  - slack.message.post

ingress:
  - event: slack.message.received
    filter:
      channel_ids: ["C123"]
    idempotency_key: "slack:message:{team_id}:{channel_id}:{message_ts}"

egress:
  - event: slack.message.post
    filter:
      channel_ids: ["C123"]
```

There is no `source` or `sink` field. The runtime resolves the owner from the event type.

If an egress event has no per-agent filter or config, we may eventually allow the `egress` section to be omitted and derive subscription from `returns`. For the first implementation, keeping an explicit `egress` section is clearer and leaves room for filtering.

## Public Interface

Keep the public interface small: one connector object with maps from event type to callables. These types live in the high-level `connectors` package, not under agent manifest code.

```python
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from zeta.events import DraftEvent, Event


IngressInput = Mapping[str, Any] | None

IngressHandler = Callable[
    ["IngressBinding", IngressInput],
    Iterable[DraftEvent] | Awaitable[Iterable[DraftEvent]],
]

EgressHandler = Callable[
    [Event, "EgressBinding", str],
    Mapping[str, Any] | None | Awaitable[Mapping[str, Any] | None],
]


@dataclass(frozen=True)
class IngressBinding:
    event: str
    filter: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None


@dataclass(frozen=True)
class EgressBinding:
    event: str
    filter: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None


@dataclass(frozen=True)
class EventConnector:
    id: str
    events: Mapping[str, Mapping[str, Any] | None] = field(default_factory=dict)
    ingress: Mapping[str, IngressHandler] = field(default_factory=dict)
    egress: Mapping[str, EgressHandler] = field(default_factory=dict)
    filters: Mapping[str, Mapping[str, Any] | None] = field(default_factory=dict)
```

`EventConnector` is the public object for an external or local event boundary.

Runtime meaning:

- `events` contributes event payload schemas.
- `ingress[event_type]` produces events of that type. The runtime passes `None` for a poll tick, or the decoded external request payload for a pushed request.
- `egress[event_type]` handles durable events of that type.
- `filters[event_type]` validates the per-agent `filter` for either ingress or egress bindings.

This keeps the connector authoring surface simple. A connector package can still implement its internals using classes, clients, cursors, and helpers, but the runtime only needs this object.

## Discovery And Enablement

Use package entry points for discovery, but require explicit connector enablement.

Example package metadata:

```toml
[project.entry-points."zeta.event_connectors"]
slack = "connectors.slack:slack_event_connector"
```

Example project/runtime config:

```yaml
event_connectors:
  - slack
```

Installed connector packages should not become active merely because they are importable. Discovery answers "what is available"; project config answers "what may run."

## Slack Connector Sketch

Slack can be the first connector because it exercises both ingress and egress.

```python
SLACK_MESSAGE_RECEIVED = "slack.message.received"
SLACK_MESSAGE_POST = "slack.message.post"


def package(client: SlackClient) -> EventConnector:
    return EventConnector(
        id="slack",
        events={
            SLACK_MESSAGE_RECEIVED: SLACK_MESSAGE_RECEIVED_SCHEMA,
            SLACK_MESSAGE_POST: SLACK_MESSAGE_POST_SCHEMA,
        },
        filters={
            SLACK_MESSAGE_RECEIVED: SLACK_CHANNEL_FILTER_SCHEMA,
            SLACK_MESSAGE_POST: SLACK_CHANNEL_FILTER_SCHEMA,
        },
        ingress={SLACK_MESSAGE_RECEIVED: slack_ingress},
        egress={
            SLACK_MESSAGE_POST: lambda event, binding, key: post_slack_message(
                client,
                event,
                binding,
                key,
            ),
        },
    )
```

Ingress should use Slack-provided stable identifiers for idempotency:

- `event_id` when available
- otherwise `team_id`, `channel_id`, and message `ts`

Egress should receive an idempotency key derived from the durable event id unless the binding overrides it.

## Runtime Behavior

Ingress:

1. Load enabled event connectors.
2. Load agent specs and validate `ingress` bindings against connector-owned event types.
3. Invoke enabled ingress handlers from a poll tick or external request.
4. Validate produced event payloads.
5. Accept produced events into the durable event log using the binding idempotency key.

Egress:

1. Load enabled event connectors.
2. Load agent specs and validate `egress` bindings against connector-owned event types.
3. Register durable egress executors for matching event types.
4. When a matching event is queued, call the connector handler.
5. Record `runtime.egress.started`.
6. Record either `runtime.egress.completed` with handler result or `runtime.egress.failed` with the error.

Egress is conceptually a subscription, but it should be implemented with the durable queue rather than only a live callback. That preserves offline recovery, retryability, idempotency, and observability.

## Tests

Start with tests before implementation.

Core tests:

- A connector can contribute event schemas.
- Agent manifests validate `ingress` and `egress` bindings by event type.
- Unknown ingress/egress event types fail validation.
- Invalid filters fail validation with the connector filter schema.
- Ingress-produced events are appended with rendered idempotency keys.
- Egress handlers are invoked for matching returned events.
- Egress failures record `runtime.egress.failed` without crashing unrelated work.
- Connector discovery lists available connectors but only enabled connectors are loaded.

Slack connector tests:

- Incoming Slack message maps to `slack.message.received`.
- Slack message ingress sets a stable session id for thread continuity.
- `slack.message.post` maps to the Slack post API payload.
- Channel filters skip or reject disallowed channels.
- Slack client failures are surfaced through `runtime.egress.failed`.

Use fake Slack clients in tests. Do not call Slack in the test suite.

## Migration From Current Shape

Current shape:

```yaml
ingress:
  - source: slack
    event: slack.dm.received

egress:
  - sink: slack
    event: slack.message.send.requested
```

Target shape:

```yaml
ingress:
  - event: slack.message.received

egress:
  - event: slack.message.post
```

Migration steps:

1. Introduce event-first bindings while tests lock current behavior.
2. Update validation to resolve connectors by event type instead of `source` or `sink`.
3. Update egress executor registration to use `binding.event`.
4. Update ingress polling to use `binding.event`.
5. Add connector discovery and explicit enablement.
6. Rename Slack event examples to the new convention.
7. Remove `source` and `sink` compatibility rather than carrying legacy behavior.

## Naming Brainstorm

`ZetaExtension` is probably too broad. Tools, skills, resources, model providers, prompt preprocessors, and UI surfaces could all reasonably be called extensions.

The name should communicate that this object is specifically about event ingress and event egress.

Options:

- `EventConnector`
  - Chosen name.
  - Good product/API feel for connecting external systems or local sources to the event log.
  - More specific than `Extension`, but not as awkward as `IngressEgressPackage`.
- `EventIOPackage`
  - Clear that it owns event input/output.
  - Slightly mechanical, but accurate.
- `EventBridge`
  - Good product feel for connecting external systems to the event log.
  - Could be confused with AWS EventBridge.
- `Integration`
  - Familiar for Slack/GitHub/Linear-style packages.
  - Too broad if a package only contributes local event I/O.
- `EventIntegration`
  - More precise than `Integration`.
  - Still external-system leaning.
- `IngressEgressPackage`
  - Extremely explicit.
  - Awkward and not pleasant as a public API.
- `EventPort`
  - Nice architecture term: ingress/egress are ports around the event log.
  - May be too abstract.
- `EventAdapter`
  - Technically accurate: adapts external systems to/from Zeta events.
  - Might imply one event type rather than a connector-owned set of event types.
- `IOProvider`
  - Short.
  - Too generic without the word event.

Decision: `EventConnector` for the public object and `event_connectors` for config/entry-point naming.

## Open Questions

- Should egress bindings be optional when `returns` includes a connector-owned egress event and no filter is needed?
- Should filters be split into `ingress_filters` and `egress_filters` if the same event type ever appears in both directions?
- Should connector enablement live in the project config, runtime config, or agent manifest?
- Should ingress support long-running async streams in addition to polling?
- Should config and entry points use `event_connectors`, or is `connectors` clear enough in project files?
