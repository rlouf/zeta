"""Send Pushover messages and Apple Watch Glance updates.

Credentials come from the environment instead of event payloads because Zeta
stores event payloads in its journal.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from zeta.effects import DeliverySemantics
from zeta.events import Event

from connectors import EgressBinding, EventConnector

PUSHOVER_MESSAGE_SEND = "pushover.message.send"
PUSHOVER_GLANCE_UPDATE = "pushover.glance.update"
PUSHOVER_MESSAGE_SEND_SEMANTICS: DeliverySemantics = "at_least_once"
PUSHOVER_GLANCE_UPDATE_SEMANTICS: DeliverySemantics = "unsafe_to_retry"

PUSHOVER_EGRESS_SEMANTICS: Mapping[str, DeliverySemantics] = {
    PUSHOVER_MESSAGE_SEND: PUSHOVER_MESSAGE_SEND_SEMANTICS,
    PUSHOVER_GLANCE_UPDATE: PUSHOVER_GLANCE_UPDATE_SEMANTICS,
}

_MESSAGE_FIELDS = (
    "message",
    "title",
    "priority",
    "sound",
    "url",
    "url_title",
    "ttl",
    "retry",
    "expire",
)
_GLANCE_FIELDS = ("title", "text", "subtext", "count", "percent")


@dataclass(frozen=True)
class HttpPushoverClient:
    """Call Pushover without adding a second client dependency."""

    token: str
    user: str
    device: str | None = None
    base_url: str = "https://api.pushover.net"
    timeout_seconds: float = 10.0
    transport: httpx.AsyncBaseTransport | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    async def send_message(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return await self._post("/1/messages.json", payload)

    async def update_glance(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return await self._post("/1/glances.json", payload)

    async def _post(
        self,
        path: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        form = {
            "token": self.token,
            "user": self.user,
            **{key: str(value) for key, value in payload.items()},
        }
        if self.device is not None:
            form["device"] = self.device

        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            transport=self.transport,
        ) as client:
            response = await client.post(
                f"{self.base_url.rstrip('/')}{path}",
                data=form,
            )

        try:
            data = response.json()
        except ValueError as exc:
            if response.is_error:
                raise RuntimeError(
                    f"Pushover returned HTTP {response.status_code} with invalid JSON"
                ) from exc
            raise RuntimeError("Pushover returned invalid JSON") from exc
        if not isinstance(data, Mapping):
            raise RuntimeError("Pushover returned a non-object response")
        if response.is_error or data.get("status") != 1:
            detail = self._safe_error_detail(data, response.status_code)
            raise RuntimeError(f"Pushover request failed: {detail}")
        if not isinstance(data.get("request"), str) or not data["request"]:
            raise RuntimeError("Pushover returned no request ID")
        return data

    def _safe_error_detail(
        self,
        data: Mapping[str, Any],
        status_code: int,
    ) -> str:
        raw_errors = data.get("errors")
        if isinstance(raw_errors, str):
            detail = raw_errors
        elif isinstance(raw_errors, list | tuple):
            detail = "; ".join(
                item for item in raw_errors if isinstance(item, str) and item
            )
        else:
            detail = ""
        if not detail:
            detail = f"HTTP {status_code}"

        # Provider errors can repeat submitted values. Do not expose credentials.
        for secret in (self.token, self.user, self.device):
            if secret:
                detail = detail.replace(secret, "<redacted>")
        return detail


def pushover_event_connector(client: Any | None = None) -> EventConnector:
    client = client if client is not None else pushover_client_from_env()
    return EventConnector(
        id="pushover",
        events={
            PUSHOVER_MESSAGE_SEND: pushover_message_send_schema(),
            PUSHOVER_GLANCE_UPDATE: pushover_glance_update_schema(),
        },
        egress={
            PUSHOVER_MESSAGE_SEND: lambda event, binding, key: send_pushover_message(
                client,
                event,
                binding,
                key,
            ),
            PUSHOVER_GLANCE_UPDATE: lambda event, binding, key: update_pushover_glance(
                client,
                event,
                binding,
                key,
            ),
        },
        egress_semantics=PUSHOVER_EGRESS_SEMANTICS,
        filters={
            PUSHOVER_MESSAGE_SEND: pushover_egress_options_schema(),
            PUSHOVER_GLANCE_UPDATE: pushover_egress_options_schema(),
        },
    )


async def send_pushover_message(
    client: Any,
    event: Event,
    binding: EgressBinding,
    idempotency_key: str,
) -> Mapping[str, Any]:
    """Send one message that can be duplicated after an ambiguous failure."""
    del binding, idempotency_key
    message = event.payload.get("message")
    if not isinstance(message, str) or not message:
        raise RuntimeError("pushover.message.send requires a non-empty message")
    payload = {
        field: event.payload[field]
        for field in _MESSAGE_FIELDS
        if field in event.payload
    }
    data = await client.send_message(payload)
    return _delivery_result(data, include_receipt=True)


async def update_pushover_glance(
    client: Any,
    event: Event,
    binding: EgressBinding,
    idempotency_key: str,
) -> Mapping[str, Any]:
    """Update only supplied fields so Pushover keeps the other Glance state."""
    del binding, idempotency_key
    payload = {
        field: event.payload[field]
        for field in _GLANCE_FIELDS
        if field in event.payload
    }
    if not payload:
        raise RuntimeError("pushover.glance.update requires at least one field")
    data = await client.update_glance(payload)
    return _delivery_result(data)


def _delivery_result(
    data: Mapping[str, Any],
    *,
    include_receipt: bool = False,
) -> Mapping[str, Any]:
    request_id = data.get("request")
    if not isinstance(request_id, str) or not request_id:
        raise RuntimeError("Pushover returned no request ID")
    result = {"request_id": request_id}
    receipt = data.get("receipt")
    if include_receipt and isinstance(receipt, str) and receipt:
        result["receipt"] = receipt
    return result


def pushover_client_from_env() -> HttpPushoverClient:
    token = os.environ.get("PUSHOVER_API_TOKEN")
    if not token:
        raise RuntimeError(
            "PUSHOVER_API_TOKEN is required for the Pushover event connector"
        )
    user = os.environ.get("PUSHOVER_USER_KEY")
    if not user:
        raise RuntimeError(
            "PUSHOVER_USER_KEY is required for the Pushover event connector"
        )
    return HttpPushoverClient(
        token=token,
        user=user,
        device=os.environ.get("PUSHOVER_DEVICE") or None,
    )


def pushover_message_send_schema() -> Mapping[str, Any]:
    return {
        "type": "object",
        "required": ["message"],
        "properties": {
            "message": {"type": "string", "minLength": 1, "maxLength": 1024},
            "title": {"type": "string", "maxLength": 250},
            "priority": {"type": "integer", "enum": [-2, -1, 0, 1, 2]},
            "sound": {"type": "string", "minLength": 1},
            "url": {"type": "string", "maxLength": 512},
            "url_title": {"type": "string", "maxLength": 100},
            "ttl": {"type": "integer", "minimum": 1},
            "retry": {"type": "integer", "minimum": 30},
            "expire": {"type": "integer", "minimum": 1, "maximum": 10800},
        },
        "allOf": [
            {
                "if": {
                    "properties": {"priority": {"const": 2}},
                    "required": ["priority"],
                },
                "then": {"required": ["retry", "expire"]},
            }
        ],
        "additionalProperties": False,
    }


def pushover_glance_update_schema() -> Mapping[str, Any]:
    clearable_count = {
        "oneOf": [
            {"type": "integer"},
            {"const": ""},
        ]
    }
    clearable_percent = {
        "oneOf": [
            {"type": "integer", "minimum": 0, "maximum": 100},
            {"const": ""},
        ]
    }
    return {
        "type": "object",
        "minProperties": 1,
        "properties": {
            "title": {"type": "string", "maxLength": 100},
            "text": {"type": "string", "maxLength": 100},
            "subtext": {"type": "string", "maxLength": 100},
            "count": clearable_count,
            "percent": clearable_percent,
        },
        "additionalProperties": False,
    }


def pushover_egress_options_schema() -> Mapping[str, Any]:
    return {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
