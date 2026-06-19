"""Turn request domain shapes."""

from dataclasses import dataclass
from typing import Literal

from zeta.kernel.events import Event

TurnOutcome = Literal["completed", "staged", "failed", "aborted"]
# `completed` covers successful turns whether they only answered or also
# applied effects.


@dataclass(frozen=True)
class TurnRequest:
    """The domain request for one agent turn.

    A turn request combines the user's task, prior timeline, optional context,
    and identity fields. Runners consume it to build prompts, call the model,
    invoke capabilities, and produce a runtime result.
    """

    task: str
    timeline: tuple[Event, ...] = ()
    context: str = ""
    session_id: str | None = None
    turn_id: str | None = None
