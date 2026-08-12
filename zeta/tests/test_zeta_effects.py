import json
from pathlib import Path

from zeta.capabilities.execution import (
    CapabilityExecutionContext,
    emit_capability_effect_event,
)
from zeta.capabilities.executors import InProcessToolExecutor
from zeta.capabilities.registry import CapabilityRegistry
from zeta.effects import effect_key

RUNTIME_VECTORS_PATH = (
    Path(__file__).resolve().parents[2] / "spec/vectors/dispatch/runtime.json"
)


def _dispatch_effect_case(name: str) -> dict:
    document = json.loads(RUNTIME_VECTORS_PATH.read_text(encoding="utf-8"))
    return next(
        case for case in document["scripted_cases"]["effects"] if case["name"] == name
    )


def test_dispatch_effect_script_freezes_durable_lifecycle() -> None:
    case = _dispatch_effect_case("unsafe_failure_becomes_ambiguous")
    drafts = []
    operation_key = effect_key(
        case["scope"],
        case["operation"],
        case["params"],
    )
    registry = CapabilityRegistry()
    context = CapabilityExecutionContext(
        event_sink=drafts.append,
        trace_store=None,
        tool_registry=registry,
        tool_executor=InProcessToolExecutor(registry),
        effect_scope=case["scope"],
    )
    for action in case["actions"]:
        emit_capability_effect_event(
            [],
            action["status"],
            capability_id=case["operation"],
            params=case["params"],
            effect_key=operation_key,
            semantics=case["semantics"],
            scope=case["scope"],
            caused_by=case["caused_by"],
            ctx=context,
            result=action.get("result"),
        )

    actual = []
    for draft in drafts:
        payload = {
            key: (
                value.replace(operation_key, "$effect")
                if isinstance(value, str)
                else value
            )
            for key, value in draft.payload.items()
        }
        actual.append(
            {
                "type": draft.event_type,
                "idempotency_key": draft.idempotency_key.replace(
                    operation_key, "$effect"
                ),
                "caused_by": draft.caused_by,
                "payload": payload,
            }
        )

    assert actual == case["expected"]


def test_effect_key_is_canonical_across_mapping_order() -> None:
    first = effect_key(
        "qi_1",
        "slack.post_message",
        {"channel": "C1", "message": {"text": "hello", "blocks": []}},
    )
    second = effect_key(
        "qi_1",
        "slack.post_message",
        {"message": {"blocks": [], "text": "hello"}, "channel": "C1"},
    )

    assert first == second
    assert first.startswith("effect:b3:")


def test_effect_key_separates_scope_operation_and_parameters() -> None:
    baseline = effect_key("qi_1", "write", {"path": "result.txt", "text": "ok"})

    assert effect_key("qi_2", "write", {"path": "result.txt", "text": "ok"}) != baseline
    assert effect_key("qi_1", "edit", {"path": "result.txt", "text": "ok"}) != baseline
    assert effect_key("qi_1", "write", {"path": "other.txt", "text": "ok"}) != baseline
