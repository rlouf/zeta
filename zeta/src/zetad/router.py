"""Pure event-to-agent routing decisions."""

from __future__ import annotations

from dataclasses import dataclass

from zeta.events import Event

from zetad.agents import AgentRoute
from zetad.queue import RoutedQueueItem, queue_item_id_for_event


@dataclass(frozen=True)
class RouteDecision:
    route: AgentRoute
    queue_item: RoutedQueueItem


@dataclass(frozen=True)
class RoutePlan:
    event: Event
    decisions: tuple[RouteDecision, ...]

    @property
    def handled(self) -> bool:
        return bool(self.decisions)


class EventRouter:
    """Match one durable event against an immutable route set."""

    def __init__(self, routes: tuple[AgentRoute, ...]) -> None:
        self.routes = routes

    def matching_routes(self, event: Event) -> tuple[AgentRoute, ...]:
        return tuple(route for route in self.routes if route.matches(event))

    def plan(self, event: Event) -> RoutePlan:
        return RoutePlan(
            event,
            tuple(
                RouteDecision(
                    route,
                    RoutedQueueItem(
                        queue_item_id=queue_item_id_for_event(route, event),
                        event_id=event.id,
                        target_agent=route.agent_id,
                        project_generation=route.project_generation,
                    ),
                )
                for route in self.matching_routes(event)
            ),
        )
