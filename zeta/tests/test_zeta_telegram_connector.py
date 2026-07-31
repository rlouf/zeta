"""Telegram connector tests."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from connectors import EgressBinding, InboundRequest
from connectors.telegram import (
    MAX_MESSAGE_CHARS,
    TELEGRAM_MESSAGE_REACTION,
    TELEGRAM_MESSAGE_RECEIVED,
    allowed_senders_from_env,
    handle_telegram_push_ingress,
    send_telegram_message,
    split_message,
    telegram_event_connector,
    telegram_message_reaction_schema,
    telegram_message_received_schema,
)
from jsonschema import Draft202012Validator
from zeta.events import Event

SECRET = "s3cret-token"
ALLOWED = frozenset({4242})


def _update(
    update_id: int = 1,
    *,
    from_id: int = 4242,
    text: str | None = "hello",
    chat_id: int = 999,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": 7,
        "date": 1_700_000_000,
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": from_id, "username": "remi"},
    }
    if text is not None:
        message["text"] = text
    return {"update_id": update_id, "message": message}


def _reaction_update(
    update_id: int = 2,
    *,
    from_id: int = 4242,
    message_id: int = 101,
    chat_id: int = 999,
    old_reaction: list[dict[str, str]] | None = None,
    new_reaction: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message_reaction": {
            "chat": {"id": chat_id, "type": "supergroup"},
            "message_id": message_id,
            "user": {"id": from_id, "username": "remi"},
            "date": 1_700_000_001,
            "old_reaction": [] if old_reaction is None else old_reaction,
            "new_reaction": (
                [{"type": "emoji", "emoji": "✅"}]
                if new_reaction is None
                else new_reaction
            ),
        },
    }


def _request(
    body: dict[str, Any] | bytes,
    *,
    secret: str | None = SECRET,
    method: str = "POST",
) -> InboundRequest:
    headers = {} if secret is None else {"x-telegram-bot-api-secret-token": secret}
    raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    return InboundRequest(
        method=method,
        path="/connectors/telegram",
        headers=headers,
        query={},
        body=raw,
    )


def _ingress(request: InboundRequest, *, allowed: frozenset[int] = ALLOWED):
    return asyncio.run(
        handle_telegram_push_ingress(
            request,
            webhook_secret=SECRET,
            allowed_senders=allowed,
        )
    )


def test_zeta_telegram_ingress_accepts_a_verified_update() -> None:
    response, drafts = _ingress(_request(_update(update_id=11)))

    assert response.status_code == 200
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.event_type == TELEGRAM_MESSAGE_RECEIVED
    assert draft.idempotency_key == "telegram:11"
    assert draft.payload["chat_id"] == 999
    assert draft.payload["from_id"] == 4242
    assert draft.payload["text"] == "hello"


def test_zeta_telegram_ingress_draft_matches_its_schema() -> None:
    _, drafts = _ingress(_request(_update()))

    Draft202012Validator(dict(telegram_message_received_schema())).validate(
        dict(drafts[0].payload)
    )


def test_zeta_telegram_ingress_accepts_a_verified_reaction() -> None:
    response, drafts = _ingress(_request(_reaction_update(update_id=12)))

    assert response.status_code == 200
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.event_type == TELEGRAM_MESSAGE_REACTION
    assert draft.idempotency_key == "telegram:12"
    assert draft.payload == {
        "update_id": 12,
        "message_id": 101,
        "chat_id": 999,
        "chat_type": "supergroup",
        "from_id": 4242,
        "from_username": "remi",
        "old_reaction": [],
        "new_reaction": [{"type": "emoji", "emoji": "✅"}],
        "date": 1_700_000_001,
    }


def test_zeta_telegram_reaction_draft_matches_its_schema() -> None:
    _, drafts = _ingress(
        _request(
            _reaction_update(
                old_reaction=[{"type": "emoji", "emoji": "👍"}],
                new_reaction=[{"type": "custom_emoji", "custom_emoji_id": "emoji-1"}],
            )
        )
    )

    Draft202012Validator(dict(telegram_message_reaction_schema())).validate(
        dict(drafts[0].payload)
    )


def test_zeta_telegram_ingress_accepts_a_reaction_removal() -> None:
    _, drafts = _ingress(
        _request(
            _reaction_update(
                old_reaction=[{"type": "emoji", "emoji": "✅"}],
                new_reaction=[],
            )
        )
    )

    assert drafts[0].payload["old_reaction"] == [{"type": "emoji", "emoji": "✅"}]
    assert drafts[0].payload["new_reaction"] == []


def test_zeta_telegram_ingress_rejects_a_wrong_secret_token() -> None:
    response, drafts = _ingress(_request(_update(), secret="wrong"))

    assert response.status_code == 401
    assert drafts == ()


def test_zeta_telegram_ingress_rejects_a_missing_secret_token() -> None:
    response, drafts = _ingress(_request(_update(), secret=None))

    assert response.status_code == 401
    assert drafts == ()


def test_zeta_telegram_ingress_drops_a_sender_that_is_not_allowed() -> None:
    response, drafts = _ingress(_request(_update(from_id=1)))

    assert response.status_code == 200  # a rejected sender is not a retryable failure
    assert drafts == ()


def test_zeta_telegram_ingress_drops_a_reaction_from_a_sender_that_is_not_allowed() -> (
    None
):
    response, drafts = _ingress(_request(_reaction_update(from_id=1)))

    assert response.status_code == 200
    assert drafts == ()


def test_zeta_telegram_ingress_drops_an_anonymous_reaction() -> None:
    update = _reaction_update()
    del update["message_reaction"]["user"]
    update["message_reaction"]["actor_chat"] = {"id": -100, "type": "supergroup"}

    response, drafts = _ingress(_request(update))

    assert response.status_code == 200
    assert drafts == ()


def test_zeta_telegram_ingress_drops_a_message_without_text() -> None:
    response, drafts = _ingress(_request(_update(text=None)))

    assert response.status_code == 200
    assert drafts == ()


def test_zeta_telegram_ingress_rejects_a_non_post_request() -> None:
    response, drafts = _ingress(_request(_update(), method="GET"))

    assert response.status_code == 405
    assert drafts == ()


def test_zeta_telegram_ingress_rejects_an_unparsable_body() -> None:
    response, drafts = _ingress(_request(b"not json"))

    assert response.status_code == 400
    assert drafts == ()


def test_zeta_telegram_redelivered_update_keeps_one_idempotency_key() -> None:
    _, first = _ingress(_request(_update(update_id=77)))
    _, again = _ingress(_request(_update(update_id=77)))

    assert first[0].idempotency_key == again[0].idempotency_key == "telegram:77"


def test_zeta_telegram_allowlist_is_empty_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_ALLOWED_SENDERS", raising=False)

    assert allowed_senders_from_env() == frozenset()


def test_zeta_telegram_allowlist_parses_a_comma_separated_list(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_SENDERS", "1, 2 ,3")

    assert allowed_senders_from_env() == frozenset({1, 2, 3})


def test_zeta_telegram_allowlist_rejects_a_non_numeric_id(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_SENDERS", "1,nobody")

    with pytest.raises(RuntimeError, match="non-numeric"):
        allowed_senders_from_env()


def test_zeta_telegram_split_keeps_a_short_message_whole() -> None:
    assert split_message("hello") == ["hello"]


def test_zeta_telegram_split_breaks_on_a_line_boundary() -> None:
    text = "a" * 4000 + "\n" + "b" * 500

    parts = split_message(text)

    assert len(parts) == 2
    assert parts[0] == "a" * 4000
    assert parts[1] == "b" * 500
    assert all(len(part) <= MAX_MESSAGE_CHARS for part in parts)


def test_zeta_telegram_split_hard_splits_text_without_a_boundary() -> None:
    text = "x" * (MAX_MESSAGE_CHARS + 10)

    parts = split_message(text)

    assert len(parts) == 2
    assert all(len(part) <= MAX_MESSAGE_CHARS for part in parts)
    assert "".join(parts) == text


class _RecordingClient:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("Telegram sendMessage failed: chat not found")
        self.calls.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_to_message_id": reply_to_message_id,
                "parse_mode": parse_mode,
            }
        )
        return {"ok": True, "result": {"message_id": 100 + len(self.calls)}}


def _send_event(payload: dict[str, Any]) -> Event:
    return Event(
        id="evt_send",
        event_type="telegram.message.send",
        source="agent:telegram-assistant",
        payload=payload,
        idempotency_key=None,
        caused_by=None,
        session_id=None,
        run_id=None,
        turn_id=None,
        timestamp_ms=1,
    )


def _egress(client: Any, payload: dict[str, Any]):
    return asyncio.run(
        send_telegram_message(
            client,
            _send_event(payload),
            EgressBinding(event="telegram.message.send"),
            "effect-key",
        )
    )


def test_zeta_telegram_egress_sends_one_message() -> None:
    client = _RecordingClient()

    result = _egress(client, {"chat_id": 999, "text": "hi"})

    assert len(client.calls) == 1
    assert client.calls[0]["chat_id"] == 999
    assert client.calls[0]["text"] == "hi"
    assert result == {"message_ids": [101]}


def test_zeta_telegram_egress_threads_only_the_first_part() -> None:
    client = _RecordingClient()
    text = "a" * 4000 + "\n" + "b" * 500

    _egress(client, {"chat_id": 999, "text": text, "reply_to_message_id": 7})

    assert len(client.calls) == 2
    assert client.calls[0]["reply_to_message_id"] == 7
    assert client.calls[1]["reply_to_message_id"] is None


def test_zeta_telegram_egress_defaults_to_plain_text() -> None:
    client = _RecordingClient()

    _egress(client, {"chat_id": 999, "text": "*not markdown*"})

    assert client.calls[0]["parse_mode"] is None


def test_zeta_telegram_egress_raises_without_a_chat_id() -> None:
    client = _RecordingClient()

    with pytest.raises(RuntimeError, match="requires chat_id and text"):
        _egress(client, {"text": "hi"})


def test_zeta_telegram_egress_propagates_a_transport_failure() -> None:
    client = _RecordingClient(fail=True)

    with pytest.raises(RuntimeError, match="chat not found"):
        _egress(client, {"chat_id": 999, "text": "hi"})


def test_zeta_telegram_connector_declares_at_least_once_delivery() -> None:
    connector = telegram_event_connector(
        _RecordingClient(),
        webhook_secret=SECRET,
        allowed_senders=ALLOWED,
    )

    assert TELEGRAM_MESSAGE_REACTION in connector.events
    assert connector.egress["telegram.message.send"] is not None
    assert connector.egress_semantics["telegram.message.send"] == "at_least_once"
    assert connector.push_ingress is not None
    assert connector.ingress == {}  # webhook only, never polled
