from zeta.events import Event
from zetad.agents import AgentRoute, EventPattern
from zetad.router import EventRouter


def test_event_router_builds_deterministic_route_plan() -> None:
    event = Event(
        id="evt_1",
        event_type="github.issue.opened",
        source="github",
        payload={},
        idempotency_key=None,
        caused_by=None,
        session_id=None,
        timestamp_ms=1,
    )
    router = EventRouter(
        (
            AgentRoute(
                "triage",
                (EventPattern("github.issue.*"),),
                project_generation="project:one",
            ),
            AgentRoute("release", (EventPattern("github.release.*"),)),
        )
    )

    plan = router.plan(event)

    assert plan.handled is True
    assert len(plan.decisions) == 1
    assert plan.decisions[0].queue_item.queue_item_id == "qi_evt_1_triage"
    assert plan.decisions[0].queue_item.project_generation == "project:one"


def test_event_router_returns_unhandled_plan_without_side_effects() -> None:
    event = Event(
        id="evt_1",
        event_type="github.issue.opened",
        source="github",
        payload={},
        idempotency_key=None,
        caused_by=None,
        session_id=None,
        timestamp_ms=1,
    )

    plan = EventRouter(()).plan(event)

    assert plan.handled is False
    assert plan.decisions == ()
