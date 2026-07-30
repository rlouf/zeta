"""Telegram event connector.

Telegram delivers updates to a webhook and accepts replies over plain HTTP, so
this connector needs no SDK and no second runtime.

Ingress is push only. Telegram retries a webhook until it receives a 200, and
Zeta appends a draft before it responds, so no cursor is needed and no message
is lost. Polling exists in the Bot API but is not used here: a confirmed
`getUpdates` offset is forgotten by Telegram, which would put a loss window
between reading an update and appending it.
"""

from __future__ import annotations

import hmac
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from zeta.effects import DeliverySemantics
from zeta.events import DraftEvent, Event

from connectors import (
    EgressBinding,
    EventConnector,
    InboundRequest,
    InboundResponse,
)

TELEGRAM_MESSAGE_RECEIVED = "telegram.message.received"
TELEGRAM_MESSAGE_SEND = "telegram.message.send"
TELEGRAM_SEND_SEMANTICS: DeliverySemantics = "at_least_once"
TELEGRAM_EGRESS_SEMANTICS: Mapping[str, DeliverySemantics] = {
    TELEGRAM_MESSAGE_SEND: TELEGRAM_SEND_SEMANTICS
}

# Telegram rejects a message longer than this, so long replies are split.
MAX_MESSAGE_CHARS = 4096
SECRET_TOKEN_HEADER = "x-telegram-bot-api-secret-token"


@dataclass(frozen=True)
class HttpTelegramClient:
    """Send messages through the Telegram Bot API."""

    token: str
    base_url: str = "https://api.telegram.org"
    timeout_seconds: float = 10.0

    async def call(self, method: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        import httpx

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url.rstrip('/')}/bot{self.token}/{method}",
                json=dict(payload),
            )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, Mapping):
            raise RuntimeError(f"Telegram {method} returned a non-object response")
        if data.get("ok") is not True:
            detail = data.get("description") or "unknown error"
            raise RuntimeError(f"Telegram {method} failed: {detail}")
        return data

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = None,
    ) -> Mapping[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        return await self.call("sendMessage", payload)


def telegram_event_connector(
    client: Any | None = None,
    *,
    webhook_secret: str | None = None,
    allowed_senders: frozenset[int] | None = None,
) -> EventConnector:
    client = client if client is not None else telegram_client_from_env()
    webhook_secret = webhook_secret or os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    senders = (
        allowed_senders if allowed_senders is not None else allowed_senders_from_env()
    )
    return EventConnector(
        id="telegram",
        events={
            TELEGRAM_MESSAGE_RECEIVED: telegram_message_received_schema(),
            TELEGRAM_MESSAGE_SEND: telegram_message_send_schema(),
        },
        push_ingress=lambda request: handle_telegram_push_ingress(
            request,
            webhook_secret=webhook_secret,
            allowed_senders=senders,
        ),
        egress={
            TELEGRAM_MESSAGE_SEND: lambda event, binding, key: send_telegram_message(
                client,
                event,
                binding,
                key,
            ),
        },
        egress_semantics=TELEGRAM_EGRESS_SEMANTICS,
        filters={TELEGRAM_MESSAGE_SEND: telegram_egress_options_schema()},
    )


async def handle_telegram_push_ingress(
    request: InboundRequest,
    *,
    webhook_secret: str | None,
    allowed_senders: frozenset[int],
) -> tuple[InboundResponse, tuple[DraftEvent, ...]]:
    """Verify one webhook request and turn it into at most one draft.

    An update Zeta will not act on is still answered with 200. Telegram retries
    any other status forever, and a rejected sender is not a transport failure.
    """
    if request.method != "POST":
        return InboundResponse(status_code=405, body=b"method not allowed"), ()
    if not valid_secret_token(request, webhook_secret=webhook_secret):
        return InboundResponse(status_code=401, body=b"invalid secret token"), ()
    try:
        update = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return InboundResponse(status_code=400, body=b"invalid payload"), ()
    if not isinstance(update, dict):
        return InboundResponse(status_code=400, body=b"invalid payload"), ()

    draft = telegram_draft_from_update(update, allowed_senders=allowed_senders)
    if draft is None:
        return InboundResponse(status_code=200, body=b"ignored"), ()
    return InboundResponse(status_code=200, body=b"accepted"), (draft,)


def valid_secret_token(
    request: InboundRequest,
    *,
    webhook_secret: str | None,
) -> bool:
    """Compare the webhook secret in constant time.

    An absent secret rejects every request. Tailscale Funnel makes the endpoint
    publicly reachable, so this check is what separates Telegram from a
    stranger.
    """
    if not webhook_secret:
        return False
    presented = request.headers.get(SECRET_TOKEN_HEADER)
    if not presented:
        return False
    return hmac.compare_digest(presented, webhook_secret)


def telegram_draft_from_update(
    update: Mapping[str, Any],
    *,
    allowed_senders: frozenset[int],
) -> DraftEvent | None:
    """Return a draft for a text message from an allowed sender, else None."""
    update_id = update.get("update_id")
    message = update.get("message")
    if not isinstance(update_id, int) or not isinstance(message, dict):
        return None

    text = message.get("text")
    chat = message.get("chat")
    sender = message.get("from")
    message_id = message.get("message_id")
    date = message.get("date")
    if not isinstance(text, str) or not text:
        return None  # a sticker, a photo, or a service message
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        return None
    if not isinstance(message_id, int) or not isinstance(date, int):
        return None

    chat_id = chat.get("id")
    from_id = sender.get("id")
    if not isinstance(chat_id, int) or not isinstance(from_id, int):
        return None
    if from_id not in allowed_senders:
        return None

    username = sender.get("username")
    payload: dict[str, Any] = {
        "update_id": update_id,
        "message_id": message_id,
        "chat_id": chat_id,
        "chat_type": str(chat.get("type") or "private"),
        "from_id": from_id,
        "from_username": username if isinstance(username, str) else None,
        "text": text,
        "date": date,
    }
    return DraftEvent(
        TELEGRAM_MESSAGE_RECEIVED,
        "telegram",
        payload,
        idempotency_key=f"telegram:{update_id}",
    )


async def send_telegram_message(
    client: Any,
    event: Event,
    binding: EgressBinding,
    idempotency_key: str,
) -> Mapping[str, Any] | None:
    """Deliver one reply, split into as many messages as Telegram allows.

    `sendMessage` takes no idempotency key, so a retry sends the text again.
    The declared `at_least_once` contract states that.
    """
    del binding, idempotency_key
    chat_id = event.payload.get("chat_id")
    text = event.payload.get("text")
    if not isinstance(chat_id, int) or not isinstance(text, str):
        raise RuntimeError("telegram.message.send requires chat_id and text")

    raw_reply_to = event.payload.get("reply_to_message_id")
    reply_to = raw_reply_to if isinstance(raw_reply_to, int) else None
    raw_parse_mode = event.payload.get("parse_mode")
    parse_mode = raw_parse_mode if isinstance(raw_parse_mode, str) else None

    message_ids: list[int] = []
    for index, chunk in enumerate(split_message(text)):
        data = await client.send_message(
            chat_id,
            chunk,
            reply_to_message_id=reply_to if index == 0 else None,
            parse_mode=parse_mode,
        )
        result = data.get("result")
        if isinstance(result, Mapping) and isinstance(result.get("message_id"), int):
            message_ids.append(int(result["message_id"]))
    return {"message_ids": message_ids}


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split text into parts Telegram will accept, preferring line breaks."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n")
        if cut <= 0:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        parts.append(remaining)
    return parts


def telegram_client_from_env() -> HttpTelegramClient:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is required for the Telegram event connector"
        )
    return HttpTelegramClient(token=token)


def allowed_senders_from_env() -> frozenset[int]:
    """Return the sender allowlist.

    An unset or empty list allows nobody. The bot endpoint is public, so a
    permissive default would expose the agent's tools to anyone who finds it.
    """
    raw = os.environ.get("TELEGRAM_ALLOWED_SENDERS", "")
    senders: set[int] = set()
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        try:
            senders.add(int(candidate))
        except ValueError as exc:
            raise RuntimeError(
                f"TELEGRAM_ALLOWED_SENDERS contains a non-numeric id: {candidate!r}"
            ) from exc
    return frozenset(senders)


def telegram_message_received_schema() -> Mapping[str, Any]:
    return {
        "type": "object",
        "required": [
            "update_id",
            "message_id",
            "chat_id",
            "chat_type",
            "from_id",
            "text",
            "date",
        ],
        "properties": {
            "update_id": {"type": "integer"},
            "message_id": {"type": "integer"},
            "chat_id": {"type": "integer"},
            "chat_type": {"type": "string"},
            "from_id": {"type": "integer"},
            "from_username": {"type": ["string", "null"]},
            "text": {"type": "string"},
            "date": {"type": "integer"},
        },
        "additionalProperties": False,
    }


def telegram_message_send_schema() -> Mapping[str, Any]:
    return {
        "type": "object",
        "required": ["chat_id", "text"],
        "properties": {
            "chat_id": {"type": "integer"},
            "text": {"type": "string"},
            "reply_to_message_id": {"type": ["integer", "null"]},
            "parse_mode": {"type": ["string", "null"]},
        },
        "additionalProperties": False,
    }


def telegram_egress_options_schema() -> Mapping[str, Any]:
    return {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
