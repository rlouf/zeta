"""Slack event connector."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from zeta.events import DraftEvent, Event

from connectors import (
    EgressBinding,
    EventConnector,
    InboundRequest,
    InboundResponse,
    IngressBinding,
    IngressInput,
)

SLACK_MESSAGE_RECEIVED = "slack.message.received"
SLACK_MESSAGE_POST = "slack.message.post"


@dataclass(frozen=True)
class HttpSlackClient:
    token: str
    base_url: str = "https://slack.com/api"
    timeout_seconds: float = 10.0

    async def post_message(
        self,
        channel_id: str,
        text: str,
        *,
        thread_ts: str | None = None,
        idempotency_key: str | None = None,
    ) -> Mapping[str, Any]:
        import httpx

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


def slack_event_connector(
    client: Any | None = None,
    *,
    signing_secret: str | None = None,
) -> EventConnector:
    client = client or slack_client_from_env()
    signing_secret = signing_secret or os.environ.get("SLACK_SIGNING_SECRET")
    return EventConnector(
        id="slack",
        events={
            SLACK_MESSAGE_RECEIVED: slack_message_received_schema(),
            SLACK_MESSAGE_POST: slack_message_post_schema(),
        },
        ingress={SLACK_MESSAGE_RECEIVED: slack_ingress},
        push_ingress=lambda request: handle_slack_push_ingress(
            request,
            signing_secret=signing_secret,
        ),
        egress={
            SLACK_MESSAGE_POST: lambda event, binding, key: post_slack_message(
                client,
                event,
                binding,
                key,
            ),
        },
        filters={
            SLACK_MESSAGE_RECEIVED: slack_ingress_filter_schema(),
            SLACK_MESSAGE_POST: slack_egress_filter_schema(),
        },
    )


async def handle_slack_push_ingress(
    request: InboundRequest,
    *,
    signing_secret: str | None,
) -> tuple[InboundResponse, tuple[DraftEvent, ...]]:
    if request.method != "POST":
        return InboundResponse(status_code=405, body=b"method not allowed"), ()
    if not valid_slack_signature(request, signing_secret=signing_secret):
        return InboundResponse(status_code=401, body=b"invalid signature"), ()
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return InboundResponse(status_code=400, body=b"invalid payload"), ()
    if not isinstance(payload, dict):
        return InboundResponse(status_code=400, body=b"invalid payload"), ()

    payload_type = payload.get("type")
    if payload_type == "url_verification":
        challenge = payload.get("challenge")
        if not isinstance(challenge, str):
            return InboundResponse(status_code=400, body=b"invalid payload"), ()
        return (
            InboundResponse(
                status_code=200,
                body=challenge.encode("utf-8"),
                headers={"Content-Type": "text/plain; charset=utf-8"},
            ),
            (),
        )
    if payload_type != "event_callback":
        return InboundResponse(status_code=202, body=b"ignored"), ()

    drafts = slack_ingress(IngressBinding(SLACK_MESSAGE_RECEIVED), payload)
    if not drafts:
        return InboundResponse(status_code=202, body=b"ignored"), ()
    return InboundResponse(status_code=202, body=b"accepted"), drafts


SLACK_SIGNATURE_MAX_AGE_SECONDS = 60 * 5


def valid_slack_signature(
    request: InboundRequest,
    *,
    signing_secret: str | None,
    now: float | None = None,
) -> bool:
    timestamp = request.headers.get("x-slack-request-timestamp")
    signature = request.headers.get("x-slack-signature")
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        request_age = abs((time.time() if now is None else now) - int(timestamp))
    except ValueError:
        return False
    if request_age > SLACK_SIGNATURE_MAX_AGE_SECONDS:
        return False
    expected = hmac.new(
        signing_secret.encode("utf-8"),
        f"v0:{timestamp}:".encode() + request.body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, f"v0={expected}")


def slack_client_from_env() -> HttpSlackClient:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN is required for the Slack event connector")
    return HttpSlackClient(token=token)


def slack_ingress(
    binding: IngressBinding,
    item: IngressInput = None,
) -> tuple[DraftEvent, ...]:
    if item is None or item.get("type") != "event_callback":
        return ()
    outer_event_id = item.get("event_id")
    event_payload = item.get("event")
    if not isinstance(outer_event_id, str) or not isinstance(event_payload, Mapping):
        return ()
    if (
        event_payload.get("bot_id") is not None
        or event_payload.get("subtype") is not None
    ):
        return ()
    if event_payload.get("type") not in {"app_mention", "message"}:
        return ()

    team_id = item.get("team_id")
    channel_id = event_payload.get("channel")
    user_id = event_payload.get("user")
    text = event_payload.get("text")
    message_ts = event_payload.get("ts")
    thread_ts = event_payload.get("thread_ts")
    required = (team_id, channel_id, user_id, text, message_ts)
    if not all(isinstance(value, str) and value for value in required):
        return ()
    if thread_ts is not None and not isinstance(thread_ts, str):
        return ()

    channels = slack_channel_ids(binding.filter)
    if channels and channel_id not in channels:
        return ()

    conversation_ts = thread_ts or message_ts
    return (
        DraftEvent(
            SLACK_MESSAGE_RECEIVED,
            "slack",
            {
                "event_id": outer_event_id,
                "team_id": team_id,
                "channel_id": channel_id,
                "message_ts": message_ts,
                "thread_ts": thread_ts,
                "user_id": user_id,
                "text": text,
            },
            idempotency_key=f"slack:event:{outer_event_id}",
            session_id=f"slack:{team_id}:{channel_id}:{conversation_ts}",
        ),
    )


async def post_slack_message(
    client: Any,
    event: Event,
    binding: EgressBinding,
    idempotency_key: str,
) -> Mapping[str, Any]:
    channel_id = required_payload_string(event.payload, "channel_id")
    text = required_payload_string(event.payload, "text")
    channels = slack_channel_ids(binding.options)
    if channels and channel_id not in channels:
        raise ValueError(
            f"Slack channel {channel_id!r} is not allowed by binding filter"
        )
    result = await client.post_message(
        channel_id,
        text,
        thread_ts=optional_payload_string(event.payload, "thread_ts"),
        idempotency_key=idempotency_key,
    )
    response_channel = optional_payload_string(result, "channel") or channel_id
    message_ts = optional_payload_string(result, "ts")
    message = result.get("message")
    if message_ts is None and isinstance(message, Mapping):
        message_ts = optional_payload_string(message, "ts")
    payload: dict[str, Any] = {"channel_id": response_channel}
    if message_ts is not None:
        payload["message_ts"] = message_ts
        payload["provider_message_id"] = f"{response_channel}:{message_ts}"
    return payload


def slack_channel_ids(value: Mapping[str, Any]) -> tuple[str, ...]:
    raw = value.get("channel_ids")
    if not isinstance(raw, list | tuple):
        return ()
    return tuple(channel for channel in raw if isinstance(channel, str) and channel)


def required_payload_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"payload field {key!r} must be a non-empty string")
    return item


def optional_payload_string(value: Mapping[str, Any], key: str) -> str | None:
    item = value.get(key)
    if isinstance(item, str) and item:
        return item
    return None


def slack_message_received_schema() -> Mapping[str, Any]:
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


def slack_message_post_schema() -> Mapping[str, Any]:
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


def slack_ingress_filter_schema() -> Mapping[str, Any]:
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


def slack_egress_filter_schema() -> Mapping[str, Any]:
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
