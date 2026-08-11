"""The Telegram connector: long-poll ingress and message egress.

Ingress long-polls `getUpdates` and confirms offsets only when the
runtime acknowledges the corresponding event (spec §7): the offset
sent to Telegram is the largest prefix of acked updates, so a crash
between reading an update and journaling it redelivers instead of
losing. Updates Zeta will not act on are confirmed immediately.

Reconnection is crash-only: a transport failure exits the process and
the runtime's supervisor respawn is the retry.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from zeta.ipc.client import EventType, SourceEvent, run_peer
from zeta.paths import resolve_state_dir

from zeta_connectors import connector_main

TELEGRAM_MESSAGE_RECEIVED = "telegram.message.received"
TELEGRAM_MESSAGE_REACTION = "telegram.message.reaction"
TELEGRAM_MESSAGE_SEND = "telegram.message.send"

# Telegram rejects a message longer than this, so long replies are split.
MAX_MESSAGE_CHARS = 4096
DEFAULT_MEDIA_MAX_BYTES = 20 * 1024 * 1024
LONG_POLL_SECONDS = 25


class TelegramMediaDownloadError(RuntimeError):
    """Raised when Telegram media cannot become a local attachment."""


class TelegramMediaTooLargeError(TelegramMediaDownloadError):
    """Raised when Telegram media exceeds the configured size limit."""


@dataclass(frozen=True)
class HttpTelegramClient:
    """Send messages through the Telegram Bot API."""

    token: str
    base_url: str = "https://api.telegram.org"
    timeout_seconds: float = 10.0

    async def call(self, method: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        timeout = self.timeout_seconds
        if method == "getUpdates":
            timeout = LONG_POLL_SECONDS + self.timeout_seconds
        async with httpx.AsyncClient(timeout=timeout) as client:
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

    async def download_file(
        self,
        file_id: str,
        destination: Path,
        *,
        max_bytes: int,
    ) -> Path:
        """Download one Telegram file atomically to a private local path."""
        if max_bytes <= 0:
            raise ValueError("Telegram media size limit must be positive")
        file_path = _remote_file_path(await self.call("getFile", {"file_id": file_id}))
        _prepare_media_directory(destination)
        if destination.is_file():
            return destination

        url = (
            f"{self.base_url.rstrip('/')}/file/bot{self.token}/"
            f"{quote(file_path, safe='/')}"
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    _guard_declared_size(response.headers, max_bytes)
                    await _write_stream_atomically(response, destination, max_bytes)
        except TelegramMediaDownloadError:
            raise
        except (OSError, httpx.HTTPError) as exc:
            raise TelegramMediaDownloadError(
                f"Telegram file download failed: {exc}"
            ) from exc
        return destination


def _remote_file_path(data: Mapping[str, Any]) -> str:
    """Return the path Telegram reports for a file, or fail loudly."""
    result = data.get("result")
    file_path = result.get("file_path") if isinstance(result, Mapping) else None
    if not isinstance(file_path, str) or not file_path:
        raise TelegramMediaDownloadError("Telegram getFile returned no file path")
    return file_path


def _prepare_media_directory(destination: Path) -> None:
    """Create the media directory privately, tolerating an existing mode."""
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(destination.parent, 0o700)
    except OSError:
        pass


def _guard_declared_size(headers: Mapping[str, str], max_bytes: int) -> None:
    """Reject a file the server already says is too large."""
    content_length = headers.get("content-length")
    if content_length is None:
        return
    try:
        declared_size = int(content_length)
    except ValueError as exc:
        raise TelegramMediaDownloadError(
            "Telegram file response has an invalid content length"
        ) from exc
    if declared_size > max_bytes:
        raise TelegramMediaTooLargeError(
            f"Telegram file exceeds the {max_bytes}-byte limit"
        )


async def _write_stream_atomically(
    response: Any,
    destination: Path,
    max_bytes: int,
) -> None:
    """Write a stream to a private temporary file, then move it into place.

    The size is checked while writing, because a server may under-report
    or omit its content length.
    """
    descriptor, raw_temporary_path = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    temporary_path: Path | None = Path(raw_temporary_path)
    try:
        os.chmod(raw_temporary_path, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            received = 0
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > max_bytes:
                    raise TelegramMediaTooLargeError(
                        f"Telegram file exceeds the {max_bytes}-byte limit"
                    )
                handle.write(chunk)
        os.replace(raw_temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def telegram_event_from_update(
    update: Mapping[str, Any],
    *,
    allowed_senders: frozenset[int],
) -> tuple[str, dict[str, Any]] | None:
    """Return (event type, payload) for an allowed update, else None."""
    update_id = update.get("update_id")
    message = update.get("message")
    if not isinstance(update_id, int):
        return None
    if isinstance(message, Mapping):
        payload = telegram_message_payload(
            update_id,
            message,
            allowed_senders=allowed_senders,
        )
        return (TELEGRAM_MESSAGE_RECEIVED, payload) if payload else None

    message_reaction = update.get("message_reaction")
    if isinstance(message_reaction, Mapping):
        payload = telegram_message_reaction_payload(
            update_id,
            message_reaction,
            allowed_senders=allowed_senders,
        )
        return (TELEGRAM_MESSAGE_REACTION, payload) if payload else None
    return None


def telegram_message_payload(
    update_id: int,
    message: Mapping[str, Any],
    *,
    allowed_senders: frozenset[int],
) -> dict[str, Any] | None:
    """Return the payload for an allowed text or media message, else None."""
    raw_text = message.get("text")
    caption = message.get("caption")
    chat = message.get("chat")
    sender = message.get("from")
    message_id = message.get("message_id")
    date = message.get("date")
    if raw_text is not None and not isinstance(raw_text, str):
        return None
    if caption is not None and not isinstance(caption, str):
        return None
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        return None
    if not isinstance(message_id, int) or not isinstance(date, int):
        return None

    attachments = telegram_message_attachments(message)
    text = raw_text if isinstance(raw_text, str) else caption or ""
    if not text and not attachments:
        return None  # a sticker or a service message

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
    if attachments:
        payload["attachments"] = attachments
    return payload


def telegram_message_attachments(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the supported attachment metadata from one Telegram message."""
    voice = telegram_file_attachment("voice", message.get("voice"))
    if voice is not None:
        return [voice]

    photo = telegram_photo_attachment(message.get("photo"))
    if photo is not None:
        return [photo]

    document = telegram_file_attachment("document", message.get("document"))
    if document is not None:
        return [document]

    audio = telegram_file_attachment("audio", message.get("audio"))
    return [audio] if audio is not None else []


def telegram_photo_attachment(value: Any) -> dict[str, Any] | None:
    """Return metadata for the largest valid Telegram photo size."""
    if not isinstance(value, list):
        return None
    photos = [
        attachment
        for item in value
        if (attachment := telegram_file_attachment("photo", item)) is not None
    ]
    if not photos:
        return None
    return max(
        photos,
        key=lambda attachment: (
            int(attachment.get("width") or 0) * int(attachment.get("height") or 0),
            int(attachment.get("file_size") or 0),
        ),
    )


def telegram_file_attachment(kind: str, value: Any) -> dict[str, Any] | None:
    """Return a supported Telegram file reference, else ``None``."""
    if not isinstance(value, Mapping):
        return None
    file_id = value.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        return None

    attachment: dict[str, Any] = {"type": kind, "file_id": file_id}
    for key in ("file_unique_id", "file_name", "mime_type"):
        item = value.get(key)
        if isinstance(item, str):
            attachment[key] = item
    for key in ("file_size", "duration", "width", "height"):
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool):
            attachment[key] = item
    return attachment


def telegram_message_reaction_payload(
    update_id: int,
    message_reaction: Mapping[str, Any],
    *,
    allowed_senders: frozenset[int],
) -> dict[str, Any] | None:
    """Return the payload for a known user's reaction, else None.

    Telegram omits ``user`` for anonymous reactions. Those updates do
    not prove a particular user confirmed an action, so the connector
    ignores them.
    """
    chat = message_reaction.get("chat")
    sender = message_reaction.get("user")
    message_id = message_reaction.get("message_id")
    date = message_reaction.get("date")
    if not isinstance(chat, Mapping) or not isinstance(sender, Mapping):
        return None
    if not isinstance(message_id, int) or not isinstance(date, int):
        return None

    chat_id = chat.get("id")
    from_id = sender.get("id")
    if not isinstance(chat_id, int) or not isinstance(from_id, int):
        return None
    if from_id not in allowed_senders:
        return None

    old_reaction = telegram_reaction_types(message_reaction.get("old_reaction"))
    new_reaction = telegram_reaction_types(message_reaction.get("new_reaction"))
    if old_reaction is None or new_reaction is None:
        return None

    username = sender.get("username")
    return {
        "update_id": update_id,
        "message_id": message_id,
        "chat_id": chat_id,
        "chat_type": str(chat.get("type") or "private"),
        "from_id": from_id,
        "from_username": username if isinstance(username, str) else None,
        "old_reaction": old_reaction,
        "new_reaction": new_reaction,
        "date": date,
    }


def telegram_reaction_types(value: Any) -> list[dict[str, str]] | None:
    """Return valid Telegram reaction types, else ``None``."""
    if not isinstance(value, list):
        return None

    reactions: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            return None
        kind = item.get("type")
        if kind == "emoji":
            emoji = item.get("emoji")
            if not isinstance(emoji, str):
                return None
            reactions.append({"type": kind, "emoji": emoji})
        elif kind == "custom_emoji":
            custom_emoji_id = item.get("custom_emoji_id")
            if not isinstance(custom_emoji_id, str):
                return None
            reactions.append({"type": kind, "custom_emoji_id": custom_emoji_id})
        elif kind == "paid":
            reactions.append({"type": kind})
        else:
            return None
    return reactions


async def materialize_attachments(
    payload: dict[str, Any],
    client: HttpTelegramClient,
    *,
    media_dir: Path,
    max_bytes: int,
) -> dict[str, Any]:
    """Copy Telegram attachments to local storage before emission."""
    raw_attachments = payload.get("attachments")
    if not isinstance(raw_attachments, list):
        return payload
    update_id = payload["update_id"]

    attachments: list[dict[str, Any]] = []
    for index, raw_attachment in enumerate(raw_attachments):
        if not isinstance(raw_attachment, Mapping):
            raise TelegramMediaDownloadError("Telegram attachment is invalid")
        attachment = dict(raw_attachment)
        file_id = attachment.get("file_id")
        if not isinstance(file_id, str) or not file_id:
            raise TelegramMediaDownloadError("Telegram attachment has no file id")
        declared_size = attachment.get("file_size")
        if isinstance(declared_size, int) and declared_size > max_bytes:
            raise TelegramMediaTooLargeError(
                f"Telegram attachment exceeds the {max_bytes}-byte limit"
            )
        destination = telegram_attachment_path(
            media_dir,
            update_id=update_id,
            index=index,
            attachment=attachment,
        )
        if not destination.is_file():
            await client.download_file(file_id, destination, max_bytes=max_bytes)
        attachment["path"] = str(destination)
        attachments.append(attachment)
    return {**payload, "attachments": attachments}


def telegram_attachment_path(
    media_dir: Path,
    *,
    update_id: int,
    index: int,
    attachment: Mapping[str, Any],
) -> Path:
    """Return the stable private path for one Telegram attachment."""
    kind = attachment.get("type")
    if not isinstance(kind, str) or not kind:
        raise TelegramMediaDownloadError("Telegram attachment has no type")
    return (
        media_dir.expanduser().resolve()
        / str(update_id)
        / (f"{index}-{kind}{telegram_attachment_suffix(attachment)}")
    )


def telegram_attachment_suffix(attachment: Mapping[str, Any]) -> str:
    """Return a safe useful suffix for a materialized Telegram attachment."""
    file_name = attachment.get("file_name")
    if isinstance(file_name, str):
        suffix = Path(file_name).suffix.lower()
        if suffix and suffix[1:].isalnum() and len(suffix) <= 10:
            return suffix
    kind = attachment.get("type")
    if not isinstance(kind, str):
        return ".bin"
    return {"voice": ".ogg", "photo": ".jpg", "audio": ".audio"}.get(
        kind,
        ".bin",
    )


class OffsetLedger:
    """Confirm getUpdates offsets only up to the acked prefix (spec §7)."""

    def __init__(self) -> None:
        self._pending: dict[int, bool] = {}
        self._confirmed: int | None = None

    def observe(self, update_id: int, *, needs_ack: bool) -> None:
        self._pending[update_id] = not needs_ack
        self._advance()

    def acked(self, update_id: int) -> None:
        if update_id in self._pending:
            self._pending[update_id] = True
            self._advance()

    def _advance(self) -> None:
        for update_id in sorted(self._pending):
            if not self._pending[update_id]:
                return
            del self._pending[update_id]
            self._confirmed = update_id

    @property
    def next_offset(self) -> int | None:
        return None if self._confirmed is None else self._confirmed + 1


async def _prepare_update(
    update: Mapping[str, Any],
    *,
    client: HttpTelegramClient,
    allowed: frozenset[int],
    bound_types: set[str],
    media_dir: Path,
    max_bytes: int,
) -> tuple[int, str, dict[str, Any]] | None:
    """Return one emittable event; an empty type means confirm-and-skip."""
    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        return None
    parsed = telegram_event_from_update(update, allowed_senders=allowed)
    if parsed is None or parsed[0] not in bound_types:
        return (update_id, "", {})
    event_type, payload = parsed
    try:
        payload = await materialize_attachments(
            payload, client, media_dir=media_dir, max_bytes=max_bytes
        )
    except TelegramMediaTooLargeError:
        return (update_id, "", {})
    return (update_id, event_type, payload)


def poll_events(config: dict[str, Any]) -> AsyncIterator[SourceEvent] | None:
    bound_types = {
        binding.get("event")
        for binding in config.get("bindings", [])
        if binding.get("event")
        in {TELEGRAM_MESSAGE_RECEIVED, TELEGRAM_MESSAGE_REACTION}
    }
    if not bound_types:
        return None  # a provider-only incarnation must not long-poll
    client = telegram_client_from_env()
    allowed = allowed_senders_from_env()
    media_dir = telegram_media_dir_from_env()
    max_bytes = telegram_media_max_bytes_from_env()

    async def events() -> AsyncIterator[SourceEvent]:
        ledger = OffsetLedger()
        emitted: set[int] = set()
        while True:
            request: dict[str, Any] = {
                "timeout": LONG_POLL_SECONDS,
                "allowed_updates": ["message", "message_reaction"],
            }
            if ledger.next_offset is not None:
                request["offset"] = ledger.next_offset
            data = await client.call("getUpdates", request)
            updates = data.get("result")
            if not isinstance(updates, list):
                raise RuntimeError("Telegram getUpdates returned no result list")
            for update in updates:
                prepared = await _prepare_update(
                    update,
                    client=client,
                    allowed=allowed,
                    bound_types=bound_types,
                    media_dir=media_dir,
                    max_bytes=max_bytes,
                )
                if prepared is None:
                    continue
                update_id, event_type, payload = prepared
                if update_id in emitted:
                    continue
                emitted.add(update_id)
                if not event_type:
                    ledger.observe(update_id, needs_ack=False)
                    continue
                ledger.observe(update_id, needs_ack=True)
                yield SourceEvent(
                    event_type,
                    payload,
                    on_ack=lambda update_id=update_id: ledger.acked(update_id),
                )

    return events()


async def send_telegram_message(
    client: HttpTelegramClient,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Deliver one reply, split into as many messages as Telegram allows.

    `sendMessage` takes no idempotency key, so a retry sends the text
    again. The declared `at_least_once` contract states that.
    """
    chat_id = payload.get("chat_id")
    text = payload.get("text")
    if not isinstance(chat_id, int) or not isinstance(text, str):
        raise RuntimeError("telegram.message.send requires chat_id and text")

    raw_reply_to = payload.get("reply_to_message_id")
    reply_to = raw_reply_to if isinstance(raw_reply_to, int) else None
    raw_parse_mode = payload.get("parse_mode")
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
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is required for the Telegram event connector"
        )
    return HttpTelegramClient(token=token)


def allowed_senders_from_env() -> frozenset[int]:
    """Return the sender allowlist.

    An unset or empty list allows nobody: a permissive default would
    expose the agent's tools to anyone who finds the bot.
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
            raise SystemExit(
                f"TELEGRAM_ALLOWED_SENDERS contains a non-numeric id: {candidate!r}"
            ) from exc
    return frozenset(senders)


def telegram_media_dir_from_env() -> Path:
    """Return the private Telegram media cache directory."""
    raw = os.environ.get("TELEGRAM_MEDIA_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    return resolve_state_dir() / "media" / "telegram"


def telegram_media_max_bytes_from_env() -> int:
    """Return the Telegram media size limit from configuration."""
    raw = os.environ.get("TELEGRAM_MEDIA_MAX_BYTES")
    if raw is None:
        return DEFAULT_MEDIA_MAX_BYTES
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit("TELEGRAM_MEDIA_MAX_BYTES must be an integer") from exc
    if value <= 0:
        raise SystemExit("TELEGRAM_MEDIA_MAX_BYTES must be positive")
    return value


def telegram_message_received_schema() -> dict[str, Any]:
    attachment_schema: dict[str, Any] = {
        "type": "object",
        "required": ["type", "file_id"],
        "properties": {
            "type": {"enum": ["voice", "photo", "document", "audio"]},
            "file_id": {"type": "string"},
            "file_unique_id": {"type": "string"},
            "file_name": {"type": "string"},
            "mime_type": {"type": "string"},
            "path": {"type": "string"},
            "file_size": {"type": "integer"},
            "duration": {"type": "integer"},
            "width": {"type": "integer"},
            "height": {"type": "integer"},
        },
        "additionalProperties": False,
    }
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
            "attachments": {
                "type": "array",
                "minItems": 1,
                "items": attachment_schema,
            },
        },
        "additionalProperties": False,
    }


def telegram_message_reaction_schema() -> dict[str, Any]:
    reaction_type_schema: dict[str, Any] = {
        "type": "object",
        "required": ["type"],
        "properties": {
            "type": {"enum": ["emoji", "custom_emoji", "paid"]},
            "emoji": {"type": "string"},
            "custom_emoji_id": {"type": "string"},
        },
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "required": [
            "update_id",
            "message_id",
            "chat_id",
            "chat_type",
            "from_id",
            "old_reaction",
            "new_reaction",
            "date",
        ],
        "properties": {
            "update_id": {"type": "integer"},
            "message_id": {"type": "integer"},
            "chat_id": {"type": "integer"},
            "chat_type": {"type": "string"},
            "from_id": {"type": "integer"},
            "from_username": {"type": ["string", "null"]},
            "old_reaction": {"type": "array", "items": reaction_type_schema},
            "new_reaction": {"type": "array", "items": reaction_type_schema},
            "date": {"type": "integer"},
        },
        "additionalProperties": False,
    }


def telegram_message_send_schema() -> dict[str, Any]:
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


def empty_options_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {}, "additionalProperties": False}


MANIFEST: dict[str, Any] = {
    "id": "telegram",
    "protocol_versions": [0],
    "events": {
        TELEGRAM_MESSAGE_RECEIVED: telegram_message_received_schema(),
        TELEGRAM_MESSAGE_REACTION: telegram_message_reaction_schema(),
        TELEGRAM_MESSAGE_SEND: telegram_message_send_schema(),
    },
    "filters": {},
    "operations": [
        {
            "name": TELEGRAM_MESSAGE_SEND,
            "semantics": "at_least_once",
            "options_schema": empty_options_schema(),
        }
    ],
    "settings": ["bindings"],
}


def run() -> None:
    client = telegram_client_from_env()

    async def send(payload: dict[str, Any], effect_key: str) -> dict[str, Any]:
        del effect_key  # sendMessage has no dedup key; semantics say so.
        return await send_telegram_message(client, payload.get("payload", {}))

    run_peer(
        poll_events,
        name="telegram",
        peer_version="0.1.0",
        event_types=[
            EventType(TELEGRAM_MESSAGE_RECEIVED, f"{TELEGRAM_MESSAGE_RECEIVED}@1"),
            EventType(TELEGRAM_MESSAGE_REACTION, f"{TELEGRAM_MESSAGE_REACTION}@1"),
        ],
        methods={TELEGRAM_MESSAGE_SEND: send},
    )


def main(argv: list[str] | None = None) -> None:
    connector_main(
        sys.argv[1:] if argv is None else argv,
        manifest=MANIFEST,
        run=run,
    )


if __name__ == "__main__":
    main()
