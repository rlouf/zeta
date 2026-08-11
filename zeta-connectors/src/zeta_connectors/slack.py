"""The Slack connector: Socket Mode ingress and message-post egress.

Ingress holds one Socket Mode websocket. The Slack envelope is
acknowledged to Slack only when the runtime acks the event (spec §7),
so Slack redelivers anything lost between the socket and the journal.
Losing the socket — including Slack's own `disconnect` refresh
request — exits the process: the supervisor respawn is the reconnect
logic, crash-only.

Secrets: `SLACK_APP_TOKEN` (xapp-, Socket Mode) and `SLACK_BOT_TOKEN`
(xoxb-, chat.postMessage), via the environment only.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from zeta.ipc.client import EventType, ProviderError, SourceEvent, run_peer

from zeta_connectors import connector_main

SLACK_MESSAGE_RECEIVED = "slack.message.received"
SLACK_MESSAGE_POST = "slack.message.post"
SLACK_API_BASE_URL = "https://slack.com/api"


@dataclass(frozen=True)
class HttpSlackClient:
    token: str
    base_url: str = SLACK_API_BASE_URL
    timeout_seconds: float = 10.0

    async def post_message(
        self,
        channel_id: str,
        text: str,
        *,
        thread_ts: str | None = None,
        idempotency_key: str | None = None,
    ) -> Mapping[str, Any]:
        payload: dict[str, Any] = {"channel": channel_id, "text": text}
        if thread_ts is not None:
            payload["thread_ts"] = thread_ts
        if idempotency_key is not None:
            payload["client_msg_id"] = idempotency_key

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url.rstrip('/')}/chat.postMessage",
                headers={"Authorization": f"Bearer {self.token}"},
                json=payload,
            )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, Mapping):
            raise RuntimeError("Slack chat.postMessage returned non-object response")
        if data.get("ok") is not True:
            error = data.get("error") or "unknown_error"
            raise RuntimeError(f"Slack chat.postMessage failed: {error}")
        return data


async def open_socket_url(
    app_token: str,
    *,
    base_url: str = SLACK_API_BASE_URL,
    timeout_seconds: float = 10.0,
) -> str:
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(
            f"{base_url.rstrip('/')}/apps.connections.open",
            headers={"Authorization": f"Bearer {app_token}"},
        )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, Mapping) or data.get("ok") is not True:
        error = data.get("error") if isinstance(data, Mapping) else "invalid response"
        raise RuntimeError(f"Slack apps.connections.open failed: {error}")
    url = data.get("url")
    if not isinstance(url, str) or not url:
        raise RuntimeError("Slack apps.connections.open returned no url")
    return url


def slack_event_from_callback(
    body: Any,
    *,
    channel_ids: tuple[str, ...],
) -> tuple[dict[str, Any], str] | None:
    """Return (payload, session id) for an accepted message, else None."""
    if not isinstance(body, Mapping) or body.get("type") != "event_callback":
        return None
    outer_event_id = body.get("event_id")
    event_payload = body.get("event")
    if not isinstance(outer_event_id, str) or not isinstance(event_payload, Mapping):
        return None
    if (
        event_payload.get("bot_id") is not None
        or event_payload.get("subtype") is not None
    ):
        return None
    if event_payload.get("type") not in {"app_mention", "message"}:
        return None

    team_id = body.get("team_id")
    channel_id = event_payload.get("channel")
    user_id = event_payload.get("user")
    text = event_payload.get("text")
    message_ts = event_payload.get("ts")
    thread_ts = event_payload.get("thread_ts")
    required = (team_id, channel_id, user_id, text, message_ts)
    if not all(isinstance(value, str) and value for value in required):
        return None
    if thread_ts is not None and not isinstance(thread_ts, str):
        return None
    if channel_ids and channel_id not in channel_ids:
        return None

    conversation_ts = thread_ts or message_ts
    payload = {
        "event_id": outer_event_id,
        "team_id": team_id,
        "channel_id": channel_id,
        "message_ts": message_ts,
        "thread_ts": thread_ts,
        "user_id": user_id,
        "text": text,
    }
    return payload, f"slack:{team_id}:{channel_id}:{conversation_ts}"


def slack_channel_ids(value: Mapping[str, Any]) -> tuple[str, ...]:
    raw = value.get("channel_ids")
    if not isinstance(raw, list | tuple):
        return ()
    return tuple(channel for channel in raw if isinstance(channel, str) and channel)


def socket_events(config: dict[str, Any]) -> AsyncIterator[SourceEvent] | None:
    bindings = [
        binding
        for binding in config.get("bindings", [])
        if binding.get("event") == SLACK_MESSAGE_RECEIVED
    ]
    if not bindings:
        return None  # a provider-only incarnation opens no socket
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not app_token:
        raise SystemExit("SLACK_APP_TOKEN is required for Slack Socket Mode ingress")
    filter_value = bindings[0].get("filter")
    channels = slack_channel_ids(
        filter_value if isinstance(filter_value, Mapping) else {}
    )

    async def events() -> AsyncIterator[SourceEvent]:
        import websockets

        url = await open_socket_url(app_token)
        async with websockets.connect(url) as socket:

            def envelope_ack(envelope_id: str) -> Any:
                return socket.send(json.dumps({"envelope_id": envelope_id}))

            async for raw in socket:
                message = json.loads(raw)
                event = socket_source_event(message, channels, envelope_ack)
                if isinstance(event, SourceEvent):
                    yield event
                elif event is not None:
                    await event  # an immediate envelope ack

    return events()


def socket_source_event(
    message: Any,
    channels: tuple[str, ...],
    envelope_ack: Any,
) -> SourceEvent | Any | None:
    """Map one Socket Mode message to an event, an ack coroutine, or None."""
    if not isinstance(message, dict):
        return None
    kind = message.get("type")
    if kind == "hello":
        return None
    if kind == "disconnect":
        # Slack refreshes sockets by asking; crash-only: exit, the
        # supervisor respawns and opens a fresh socket.
        raise RuntimeError("slack requested a socket refresh")
    envelope_id = message.get("envelope_id")
    if not isinstance(envelope_id, str):
        return None
    parsed = (
        slack_event_from_callback(message.get("payload"), channel_ids=channels)
        if kind == "events_api"
        else None
    )
    if parsed is None:
        return envelope_ack(envelope_id)
    payload, session_id = parsed
    return SourceEvent(
        SLACK_MESSAGE_RECEIVED,
        payload,
        session_id=session_id,
        on_ack=lambda envelope_id=envelope_id: envelope_ack(envelope_id),
    )


async def post_slack_message(
    client: HttpSlackClient,
    payload: Mapping[str, Any],
    options: Mapping[str, Any],
    effect_key: str,
) -> dict[str, Any]:
    channel_id = payload.get("channel_id")
    text = payload.get("text")
    if not isinstance(channel_id, str) or not isinstance(text, str):
        raise ProviderError(
            "schema",
            "slack.message.post requires channel_id and text",
            retryable=False,
        )
    channels = slack_channel_ids(options)
    if channels and channel_id not in channels:
        raise ProviderError(
            "schema",
            f"Slack channel {channel_id!r} is not allowed by binding options",
            retryable=False,
        )
    raw_thread = payload.get("thread_ts")
    result = await client.post_message(
        channel_id,
        text,
        thread_ts=raw_thread if isinstance(raw_thread, str) and raw_thread else None,
        idempotency_key=effect_key,
    )
    response_channel = result.get("channel")
    if not isinstance(response_channel, str) or not response_channel:
        response_channel = channel_id
    message_ts = result.get("ts")
    message = result.get("message")
    if not isinstance(message_ts, str) and isinstance(message, Mapping):
        message_ts = message.get("ts")
    delivered: dict[str, Any] = {"channel_id": response_channel}
    if isinstance(message_ts, str) and message_ts:
        delivered["message_ts"] = message_ts
        delivered["provider_message_id"] = f"{response_channel}:{message_ts}"
    return delivered


def slack_message_received_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "event_id",
            "team_id",
            "channel_id",
            "message_ts",
            "user_id",
            "text",
        ],
        "properties": {
            "event_id": {"type": "string"},
            "team_id": {"type": "string"},
            "channel_id": {"type": "string"},
            "message_ts": {"type": "string"},
            "thread_ts": {"type": ["string", "null"]},
            "user_id": {"type": "string"},
            "text": {"type": "string"},
        },
        "additionalProperties": False,
    }


def slack_message_post_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["channel_id", "text"],
        "properties": {
            "channel_id": {"type": "string"},
            "thread_ts": {"type": "string"},
            "text": {"type": "string"},
        },
        "additionalProperties": False,
    }


def slack_ingress_filter_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["channel_ids"],
        "properties": {
            "channel_ids": {
                "type": "array",
                "items": {"type": "string"},
            }
        },
        "additionalProperties": False,
    }


def slack_egress_options_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "channel_ids": {
                "type": "array",
                "items": {"type": "string"},
            }
        },
        "additionalProperties": False,
    }


MANIFEST: dict[str, Any] = {
    "id": "slack",
    "protocol_versions": [0],
    "events": {
        SLACK_MESSAGE_RECEIVED: slack_message_received_schema(),
        SLACK_MESSAGE_POST: slack_message_post_schema(),
    },
    "filters": {SLACK_MESSAGE_RECEIVED: slack_ingress_filter_schema()},
    "operations": [
        {
            "name": SLACK_MESSAGE_POST,
            "semantics": "connector_deduplicated",
            "options_schema": slack_egress_options_schema(),
        }
    ],
    "settings": ["bindings"],
}


def run() -> None:
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        raise SystemExit("SLACK_BOT_TOKEN is required for the Slack event connector")
    client = HttpSlackClient(token=bot_token)

    async def post(payload: dict[str, Any], effect_key: str) -> dict[str, Any]:
        return await post_slack_message(
            client,
            payload.get("payload", {}),
            payload.get("options", {}),
            effect_key,
        )

    run_peer(
        socket_events,
        name="slack",
        peer_version="0.1.0",
        event_types=[EventType(SLACK_MESSAGE_RECEIVED, f"{SLACK_MESSAGE_RECEIVED}@1")],
        methods={SLACK_MESSAGE_POST: post},
    )


def main(argv: list[str] | None = None) -> None:
    connector_main(
        sys.argv[1:] if argv is None else argv,
        manifest=MANIFEST,
        run=run,
    )


if __name__ == "__main__":
    main()
