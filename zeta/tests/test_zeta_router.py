from zeta.events import Event
from zeta.harness.router import EventRouter
from zeta.harness.routing import AgentRoute, EventPattern


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


def test_event_router_resolves_shared_session_before_execution() -> None:
    event = Event(
        id="evt_1",
        event_type="work.requested",
        source="test",
        payload={},
        idempotency_key=None,
        caused_by=None,
        session_id=None,
        timestamp_ms=1,
    )

    plan = EventRouter(
        (AgentRoute("worker", (EventPattern("work.requested"),), session="shared"),)
    ).plan(event)

    assert plan.decisions[0].queue_item.session_id == "agent/worker"


def test_event_router_resolves_per_event_session_before_execution() -> None:
    event = Event(
        id="evt_1",
        event_type="work.requested",
        source="test",
        payload={},
        idempotency_key=None,
        caused_by=None,
        session_id=None,
        timestamp_ms=1,
    )

    plan = EventRouter(
        (
            AgentRoute(
                "worker",
                (EventPattern("work.requested"),),
                session="per-event",
            ),
        )
    ).plan(event)

    assert plan.decisions[0].queue_item.session_id == "agent/worker/evt_1"


def test_event_router_resolves_templated_session_before_execution() -> None:
    event = Event(
        id="evt_1",
        event_type="work.requested",
        source="test",
        payload={"thread_id": "thread-7"},
        idempotency_key=None,
        caused_by=None,
        session_id=None,
        timestamp_ms=1,
    )

    plan = EventRouter(
        (
            AgentRoute(
                "worker",
                (EventPattern("work.requested"),),
                session="{thread_id}",
            ),
        )
    ).plan(event)

    assert plan.decisions[0].queue_item.session_id == "agent/worker/thread-7"
