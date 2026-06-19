"""Agent declaration domain shapes."""

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Literal

from zeta.kernel.events import Event

DispatchMode = Literal["one_shot", "session_scoped"]


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

    def accepts(self, event: Event) -> bool:
        return any(trigger.matches(event) for trigger in self.triggers)


@dataclass(frozen=True)
class AgentInvocation:
    """The domain input for one event-triggered agent invocation.

    Agent runners receive the definition that matched and the durable event
    that triggered the run.
    """

    agent: AgentDefinition
    triggering_event: Event
