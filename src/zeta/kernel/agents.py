"""Agent declaration domain shapes."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Literal

from zeta.kernel.events import DraftEvent, Event

DispatchMode = Literal["one_shot", "session_scoped"]
AgentEventPublisher = Callable[[DraftEvent], Awaitable[Event]]


@dataclass(frozen=True)
class EventPattern:
    """A glob pattern that matches event types which can start an agent.

    The pattern uses normal shell-style glob matching. `github.issue.*` matches
    any issue event, while `session.turn.requested` matches exactly that event
    type.
    """

    event_type: str

    def matches(self, event: Event) -> bool:
        return fnmatchcase(event.event_type, self.event_type)


@dataclass(frozen=True)
class AgentDefinition:
    """A declarative description of an event-triggered agent.

    Agent definitions describe what an agent is, which events can start it, and
    which capabilities and prompt constraints shape its turn. Dispatch
    registration attaches executable runner code separately.
    """

    agent_id: str
    triggers: tuple[EventPattern, ...]
    allowed_capabilities: tuple[str, ...] = ()
    system_prompt: str | None = None
    max_turns: int | None = None
    dispatch_mode: DispatchMode = "one_shot"
    returns: tuple[str, ...] = ()
    lock_keys: tuple[str, ...] = ()

    def accepts(self, event: Event) -> bool:
        return any(trigger.matches(event) for trigger in self.triggers)


@dataclass(frozen=True)
class AgentInvocation:
    """The dispatch context passed to one event-triggered agent invocation.

    Runners receive the matched definition, the durable triggering event, and
    optional queue/attempt/run ids. The explicit context keeps routing metadata
    out of domain payloads while still letting agents publish correlated events.
    """

    agent: AgentDefinition
    triggering_event: Event
    publish_event: AgentEventPublisher | None = None
    queue_item_id: str | None = None
    attempt_id: str | None = None
    run_id: str | None = None

    async def publish(self, draft: DraftEvent) -> Event:
        if self.publish_event is None:
            raise RuntimeError("agent invocation cannot publish events")
        return await self.publish_event(draft)
