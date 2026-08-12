"""The Pushover connector: pure egress to the Pushover app.

Credentials come from the environment because event payloads and
envelopes are journaled.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from zeta.ipc.client import ProviderError, run_peer

from zeta_connectors import connector_main

PUSHOVER_MESSAGE_SEND = "pushover.message.send"
PUSHOVER_GLANCE_UPDATE = "pushover.glance.update"

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


async def send_pushover_message(
    client: HttpPushoverClient,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Send one message that can be duplicated after an ambiguous failure."""
    message = payload.get("message")
    if not isinstance(message, str) or not message:
        raise ProviderError(
            "schema",
            "pushover.message.send requires a non-empty message",
            retryable=False,
        )
    form = {field: payload[field] for field in _MESSAGE_FIELDS if field in payload}
    data = await client.send_message(form)
    return _delivery_result(data, include_receipt=True)


async def update_pushover_glance(
    client: HttpPushoverClient,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Update only supplied fields so Pushover keeps the other Glance state."""
    form = {field: payload[field] for field in _GLANCE_FIELDS if field in payload}
    if not form:
        raise ProviderError(
            "schema",
            "pushover.glance.update requires at least one field",
            retryable=False,
        )
    data = await client.update_glance(form)
    return _delivery_result(data)


def _delivery_result(
    data: Mapping[str, Any],
    *,
    include_receipt: bool = False,
) -> dict[str, Any]:
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
        raise SystemExit(
            "PUSHOVER_API_TOKEN is required for the Pushover event connector"
        )
    user = os.environ.get("PUSHOVER_USER_KEY")
    if not user:
        raise SystemExit(
            "PUSHOVER_USER_KEY is required for the Pushover event connector"
        )
    return HttpPushoverClient(
        token=token,
        user=user,
        device=os.environ.get("PUSHOVER_DEVICE") or None,
    )


def pushover_message_send_schema() -> dict[str, Any]:
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


def pushover_glance_update_schema() -> dict[str, Any]:
    clearable_count = {
        "oneOf": [
            {"type": "integer"},
            {"type": "null"},
        ]
    }
    clearable_percent = {
        "oneOf": [
            {"type": "integer", "minimum": 0, "maximum": 100},
            {"type": "null"},
        ]
    }
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string", "maxLength": 100},
            "text": {"type": "string", "maxLength": 100},
            "subtext": {"type": "string", "maxLength": 100},
            "count": clearable_count,
            "percent": clearable_percent,
        },
        "additionalProperties": False,
        "minProperties": 1,
    }


def empty_options_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {}, "additionalProperties": False}


MANIFEST: dict[str, Any] = {
    "id": "pushover",
    "protocol_versions": [0],
    "events": {
        PUSHOVER_MESSAGE_SEND: pushover_message_send_schema(),
        PUSHOVER_GLANCE_UPDATE: pushover_glance_update_schema(),
    },
    "filters": {},
    "operations": [
        {
            "name": PUSHOVER_MESSAGE_SEND,
            "semantics": "at_least_once",
            "options_schema": empty_options_schema(),
        },
        {
            "name": PUSHOVER_GLANCE_UPDATE,
            "semantics": "unsafe_to_retry",
            "options_schema": empty_options_schema(),
        },
    ],
    "settings": [],
}


def run() -> None:
    client = pushover_client_from_env()

    async def send(
        payload: dict[str, Any],
        base_dir: str | None,
        effect_key: str | None,
    ) -> dict[str, Any]:
        del base_dir, effect_key  # Pushover offers no dedup key; semantics say so.
        return await send_pushover_message(client, payload.get("payload", {}))

    async def glance(
        payload: dict[str, Any],
        base_dir: str | None,
        effect_key: str | None,
    ) -> dict[str, Any]:
        del base_dir, effect_key
        return await update_pushover_glance(client, payload.get("payload", {}))

    run_peer(
        None,
        name="pushover",
        peer_version="0.1.0",
        methods={
            PUSHOVER_MESSAGE_SEND: send,
            PUSHOVER_GLANCE_UPDATE: glance,
        },
    )


def main(argv: list[str]) -> None:
    connector_main(argv, manifest=MANIFEST, run=run)
