from zeta.effects import effect_key


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
