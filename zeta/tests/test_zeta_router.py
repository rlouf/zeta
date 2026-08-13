import json
from pathlib import Path

from zeta.events import Event
from zeta.harness.router import EventRouter
from zeta.harness.routing import AgentRoute, EventPattern

RUNTIME_VECTORS_PATH = (
    Path(__file__).resolve().parents[2] / "spec/vectors/dispatch/runtime.json"
)


def _runtime_vectors() -> dict:
    return json.loads(RUNTIME_VECTORS_PATH.read_text(encoding="utf-8"))


def _event_from_vector(value: dict) -> Event:
    return Event(
        id=value["id"],
        event_type=value["type"],
        source=value["source"],
        payload=value["payload"],
        idempotency_key=value.get("idempotency_key"),
        caused_by=value.get("caused_by"),
        session_id=value.get("session_id"),
        run_id=value.get("run_id"),
        turn_id=value.get("turn_id"),
        timestamp_ms=value["timestamp_ms"],
    )


def _route_from_vector(value: dict) -> AgentRoute:
    return AgentRoute(
        agent_id=value["agent_id"],
        accepts=tuple(EventPattern(pattern) for pattern in value["accepts"]),
        session=value.get("session", "per-event"),
        project_revision=value.get("project_revision"),
    )


def test_event_router_matches_dispatch_route_vectors_in_declaration_order() -> None:
    for case in _runtime_vectors()["route_cases"]:
        router = EventRouter(
            tuple(_route_from_vector(route) for route in case["routes"])
        )

        plan = router.plan(_event_from_vector(case["event"]))
        actual = [
            {
                "agent_id": decision.route.agent_id,
                "queue_item_id": decision.queue_item.queue_item_id,
                "session_id": decision.queue_item.session_id,
                "project_revision": decision.queue_item.project_revision,
            }
            for decision in plan.decisions
        ]

        assert actual == case["expected_decisions"], case["name"]
        assert plan.handled is bool(case["expected_decisions"]), case["name"]


def test_event_router_matches_dispatch_session_vectors() -> None:
    for case in _runtime_vectors()["session_cases"]:
        route = AgentRoute(
            case["agent_id"],
            (EventPattern(case["event"]["type"]),),
            session=case["session"],
        )

        plan = EventRouter((route,)).plan(_event_from_vector(case["event"]))

        assert plan.decisions[0].queue_item.session_id == case["expected_session_id"], (
            case["name"]
        )


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
                project_revision="project:one",
            ),
            AgentRoute("release", (EventPattern("github.release.*"),)),
        )
    )

    plan = router.plan(event)

    assert plan.handled is True
    assert len(plan.decisions) == 1
    assert plan.decisions[0].queue_item.queue_item_id == "qi_evt_1_triage"
    assert plan.decisions[0].queue_item.project_revision == "project:one"


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
