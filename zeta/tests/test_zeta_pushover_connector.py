"""Pushover connector tests."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Mapping
from typing import Any, cast
from urllib.parse import parse_qs

import httpx
import pytest
from connectors import EgressBinding
from connectors.pushover import (
    PUSHOVER_GLANCE_UPDATE,
    PUSHOVER_MESSAGE_SEND,
    HttpPushoverClient,
    pushover_client_from_env,
    pushover_event_connector,
    pushover_glance_update_schema,
    pushover_message_send_schema,
)
from jsonschema import Draft202012Validator, ValidationError
from zeta.events import Event


class _RecordingPushoverClient:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.glances: list[dict[str, Any]] = []

    async def send_message(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.messages.append(dict(payload))
        return {"status": 1, "request": "message-request", "receipt": "receipt-1"}

    async def update_glance(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.glances.append(dict(payload))
        return {"status": 1, "request": "glance-request"}


def _event(event_type: str, payload: Mapping[str, Any]) -> Event:
    return Event(
        id="evt_pushover",
        event_type=event_type,
        source="agent:notifier",
        payload=payload,
        idempotency_key=None,
        caused_by=None,
        session_id=None,
        timestamp_ms=1,
    )


def _deliver(
    client: _RecordingPushoverClient,
    event_type: str,
    payload: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    connector = pushover_event_connector(client)

    async def invoke() -> Mapping[str, Any] | None:
        result = connector.egress[event_type](
            _event(event_type, payload),
            EgressBinding(event=event_type),
            "effect-key",
        )
        if inspect.isawaitable(result):
            return await cast(Awaitable[Mapping[str, Any] | None], result)
        return cast(Mapping[str, Any] | None, result)

    return asyncio.run(invoke())


def test_zeta_pushover_connector_declares_both_egress_events() -> None:
    connector = pushover_event_connector(_RecordingPushoverClient())

    assert set(connector.events) == {
        PUSHOVER_MESSAGE_SEND,
        PUSHOVER_GLANCE_UPDATE,
    }
    assert set(connector.egress) == set(connector.events)
    assert connector.egress_semantics == {
        PUSHOVER_MESSAGE_SEND: "at_least_once",
        PUSHOVER_GLANCE_UPDATE: "unsafe_to_retry",
    }
    assert connector.ingress == {}
    assert connector.push_ingress is None


@pytest.mark.parametrize(
    "payload",
    [
        {"message": "hello"},
        {
            "message": "hello",
            "title": "Build complete",
            "priority": 2,
            "sound": "pushover",
            "url": "zeta://run/42",
            "url_title": "Open run",
            "ttl": 3600,
            "retry": 30,
            "expire": 600,
        },
    ],
)
def test_zeta_pushover_message_schema_accepts_supported_payloads(
    payload: dict[str, Any],
) -> None:
    Draft202012Validator(dict(pushover_message_send_schema())).validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"message": ""},
        {"message": "x" * 1025},
        {"message": "hello", "priority": 2},
        {"message": "hello", "priority": 3},
        {"message": "hello", "unknown": True},
    ],
)
def test_zeta_pushover_message_schema_rejects_invalid_payloads(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        Draft202012Validator(dict(pushover_message_send_schema())).validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "Status"},
        {"text": "Ready", "count": -2, "percent": 0},
        {"title": "", "text": "", "subtext": "", "count": "", "percent": ""},
    ],
)
def test_zeta_pushover_glance_schema_accepts_partial_updates_and_clears(
    payload: dict[str, Any],
) -> None:
    Draft202012Validator(dict(pushover_glance_update_schema())).validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"title": "x" * 101},
        {"count": None},
        {"percent": -1},
        {"percent": 101},
        {"unknown": "value"},
    ],
)
def test_zeta_pushover_glance_schema_rejects_invalid_updates(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        Draft202012Validator(dict(pushover_glance_update_schema())).validate(payload)


def test_zeta_pushover_message_egress_forwards_present_fields() -> None:
    client = _RecordingPushoverClient()
    payload = {
        "message": "The build is ready.",
        "title": "Zeta",
        "priority": 0,
        "ttl": 3600,
    }

    result = _deliver(client, PUSHOVER_MESSAGE_SEND, payload)

    assert client.messages == [payload]
    assert result == {"request_id": "message-request", "receipt": "receipt-1"}


def test_zeta_pushover_message_egress_requires_a_message() -> None:
    with pytest.raises(RuntimeError, match="requires a non-empty message"):
        _deliver(_RecordingPushoverClient(), PUSHOVER_MESSAGE_SEND, {})


def test_zeta_pushover_glance_egress_preserves_clears_and_zero() -> None:
    client = _RecordingPushoverClient()
    payload = {"text": "", "count": -1, "percent": 0}

    result = _deliver(client, PUSHOVER_GLANCE_UPDATE, payload)

    assert client.glances == [payload]
    assert result == {"request_id": "glance-request"}


def test_zeta_pushover_glance_egress_requires_a_field() -> None:
    with pytest.raises(RuntimeError, match="requires at least one field"):
        _deliver(_RecordingPushoverClient(), PUSHOVER_GLANCE_UPDATE, {})


def test_zeta_pushover_client_reads_credentials_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PUSHOVER_API_TOKEN", "app-token")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "user-key")
    monkeypatch.setenv("PUSHOVER_DEVICE", "watch")

    client = pushover_client_from_env()

    assert client.token == "app-token"
    assert client.user == "user-key"
    assert client.device == "watch"


def test_zeta_pushover_client_requires_both_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PUSHOVER_API_TOKEN", raising=False)
    monkeypatch.delenv("PUSHOVER_USER_KEY", raising=False)

    with pytest.raises(RuntimeError, match="PUSHOVER_API_TOKEN"):
        pushover_client_from_env()

    monkeypatch.setenv("PUSHOVER_API_TOKEN", "app-token")
    with pytest.raises(RuntimeError, match="PUSHOVER_USER_KEY"):
        pushover_client_from_env()


def test_zeta_http_pushover_client_posts_form_data() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": 1, "request": "request-1"})

    client = HttpPushoverClient(
        token="app-token",
        user="user-key",
        device="watch",
        transport=httpx.MockTransport(respond),
    )

    result = asyncio.run(client.send_message({"message": "hello"}))

    assert result == {"status": 1, "request": "request-1"}
    assert requests[0].url.path == "/1/messages.json"
    assert parse_qs(requests[0].content.decode()) == {
        "token": ["app-token"],
        "user": ["user-key"],
        "device": ["watch"],
        "message": ["hello"],
    }


def test_zeta_http_pushover_client_reports_provider_errors_without_secrets() -> None:
    token = "secret-app-token"
    user = "secret-user-key"

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "status": 0,
                "request": "failed-request",
                "errors": [f"invalid token {token} for {user}"],
            },
        )

    client = HttpPushoverClient(
        token=token,
        user=user,
        transport=httpx.MockTransport(respond),
    )

    with pytest.raises(RuntimeError) as error:
        asyncio.run(client.update_glance({"text": "hello"}))

    assert "invalid token" in str(error.value)
    assert token not in str(error.value)
    assert user not in str(error.value)


def test_zeta_http_pushover_client_propagates_a_transport_failure() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Pushover is unavailable", request=request)

    client = HttpPushoverClient(
        token="app-token",
        user="user-key",
        transport=httpx.MockTransport(fail),
    )

    with pytest.raises(httpx.ConnectError, match="unavailable"):
        asyncio.run(client.send_message({"message": "hello"}))


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(200, text="not json"), "invalid JSON"),
        (httpx.Response(200, json=[]), "non-object"),
        (httpx.Response(200, json={"status": 1}), "request ID"),
    ],
)
def test_zeta_http_pushover_client_rejects_invalid_responses(
    response: httpx.Response,
    message: str,
) -> None:
    client = HttpPushoverClient(
        token="app-token",
        user="user-key",
        transport=httpx.MockTransport(lambda _request: response),
    )

    with pytest.raises(RuntimeError, match=message):
        asyncio.run(client.send_message({"message": "hello"}))
