"""Tests for the IPC connector child processes in zeta-connectors."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from collections.abc import Callable
from inspect import Parameter, signature
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs

import httpx
import pytest
from jsonschema import Draft202012Validator
from zeta.ipc.client import ProviderError
from zeta_connectors.filesystem import (
    FILE_CREATED,
    collect_file_created,
)
from zeta_connectors.filesystem import (
    MANIFEST as FILESYSTEM_MANIFEST,
)
from zeta_connectors.filesystem import (
    main as filesystem_main,
)
from zeta_connectors.pushover import (
    PUSHOVER_GLANCE_UPDATE,
    PUSHOVER_MESSAGE_SEND,
    HttpPushoverClient,
    pushover_client_from_env,
    send_pushover_message,
    update_pushover_glance,
)
from zeta_connectors.pushover import main as pushover_main
from zeta_connectors.slack import (
    SLACK_MESSAGE_POST,
    SLACK_MESSAGE_RECEIVED,
    HttpSlackClient,
    post_slack_message,
    slack_event_from_callback,
)
from zeta_connectors.slack import main as slack_main
from zeta_connectors.telegram import (
    MAX_MESSAGE_CHARS,
    TELEGRAM_MESSAGE_REACTION,
    TELEGRAM_MESSAGE_RECEIVED,
    TELEGRAM_MESSAGE_SEND,
    HttpTelegramClient,
    OffsetLedger,
    TelegramMediaDownloadError,
    TelegramMediaTooLargeError,
    allowed_senders_from_env,
    materialize_attachments,
    send_telegram_message,
    split_message,
    telegram_event_from_update,
    telegram_media_max_bytes_from_env,
    telegram_message_reaction_schema,
    telegram_message_received_schema,
)
from zeta_connectors.telegram import main as telegram_main

ALLOWED = frozenset({4242})


def test_connector_package_registers_exact_event_connector_entry_points() -> None:
    package_metadata = tomllib.loads(
        (Path(__file__).parents[2] / "zeta-connectors" / "pyproject.toml").read_text()
    )["project"]

    assert "scripts" not in package_metadata
    assert package_metadata["entry-points"]["zeta.event_connectors"] == {
        "filesystem": "zeta_connectors.filesystem:main",
        "pushover": "zeta_connectors.pushover:main",
        "slack": "zeta_connectors.slack:main",
        "telegram": "zeta_connectors.telegram:main",
    }


@pytest.mark.parametrize(
    "entry_point",
    [filesystem_main, pushover_main, slack_main, telegram_main],
)
def test_connector_entry_point_requires_argv(
    entry_point: Callable[[list[str]], None],
) -> None:
    argv = signature(entry_point).parameters["argv"]

    assert argv.default is Parameter.empty


def _describe(connector_id: str) -> dict[str, Any]:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "zeta.ipc.client",
            "zeta.event_connectors",
            connector_id,
            f"zeta_connectors.{connector_id}:main",
            "--describe",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    manifest = json.loads(completed.stdout)
    assert isinstance(manifest, dict)
    return manifest


# --- filesystem -------------------------------------------------------------


def _make_file(directory: Path, name: str, *, mtime: float) -> Path:
    path = directory / name
    path.write_bytes(b"x")
    os.utime(path, (mtime, mtime))
    return path


def _watch(directory: Path, glob: str | None = None) -> dict[str, str]:
    watch = {"dir": str(directory)}
    if glob is not None:
        watch["glob"] = glob
    return watch


def test_filesystem_emits_for_file_created_after_watermark(tmp_path: Path) -> None:
    state: dict[str, dict[str, Any]] = {}
    collect_file_created(_watch(tmp_path), state, now=1000.0, debounce_seconds=2.0)

    _make_file(tmp_path, "note.md", mtime=1005.0)
    payloads = collect_file_created(
        _watch(tmp_path), state, now=1010.0, debounce_seconds=2.0
    )

    assert payloads == [
        {
            "path": str(tmp_path / "note.md"),
            "name": "note.md",
            "dir": str(tmp_path),
        }
    ]


def test_filesystem_ignores_pre_watermark_file(tmp_path: Path) -> None:
    _make_file(tmp_path, "old.md", mtime=500.0)
    state: dict[str, dict[str, Any]] = {}

    payloads = collect_file_created(
        _watch(tmp_path), state, now=1000.0, debounce_seconds=0.0
    )

    assert payloads == []


def test_filesystem_honors_glob_filter(tmp_path: Path) -> None:
    state: dict[str, dict[str, Any]] = {}
    collect_file_created(
        _watch(tmp_path, "*.md"), state, now=1000.0, debounce_seconds=0.0
    )
    _make_file(tmp_path, "a.md", mtime=1005.0)
    _make_file(tmp_path, "b.txt", mtime=1005.0)

    payloads = collect_file_created(
        _watch(tmp_path, "*.md"), state, now=1010.0, debounce_seconds=0.0
    )

    assert [payload["name"] for payload in payloads] == ["a.md"]


def test_filesystem_does_not_reemit_seen_file(tmp_path: Path) -> None:
    state: dict[str, dict[str, Any]] = {}
    collect_file_created(_watch(tmp_path), state, now=1000.0, debounce_seconds=0.0)
    _make_file(tmp_path, "a.md", mtime=1005.0)
    collect_file_created(_watch(tmp_path), state, now=1010.0, debounce_seconds=0.0)

    payloads = collect_file_created(
        _watch(tmp_path), state, now=1020.0, debounce_seconds=0.0
    )

    assert payloads == []


def test_filesystem_skips_file_still_being_written(tmp_path: Path) -> None:
    state: dict[str, dict[str, Any]] = {}
    collect_file_created(_watch(tmp_path), state, now=1000.0, debounce_seconds=5.0)
    _make_file(tmp_path, "a.md", mtime=1008.0)

    assert (
        collect_file_created(_watch(tmp_path), state, now=1010.0, debounce_seconds=5.0)
        == []
    )
    settled = collect_file_created(
        _watch(tmp_path), state, now=1020.0, debounce_seconds=5.0
    )
    assert len(settled) == 1


def test_filesystem_describe_reports_the_manifest() -> None:
    manifest = _describe("filesystem")

    assert manifest["id"] == "filesystem"
    assert set(manifest["events"]) == {FILE_CREATED}
    assert manifest["filters"][FILE_CREATED]["required"] == ["dir"]
    assert set(manifest["filters"][FILE_CREATED]["properties"]) == {"dir", "glob"}
    assert manifest["operations"] == []
    assert manifest == FILESYSTEM_MANIFEST


# --- telegram ---------------------------------------------------------------


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
    old_reaction: list[dict[str, str]] | None = None,
    new_reaction: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message_reaction": {
            "chat": {"id": 999, "type": "supergroup"},
            "message_id": 101,
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


def _media_update(
    media_type: str,
    media: Any,
    *,
    update_id: int = 3,
    caption: str | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": 8,
        "date": 1_700_000_002,
        "chat": {"id": 999, "type": "private"},
        "from": {"id": 4242, "username": "remi"},
        media_type: media,
    }
    if caption is not None:
        message["caption"] = caption
    return {"update_id": update_id, "message": message}


class _MediaClient:
    def __init__(self, fail: bool = False) -> None:
        self.file_ids: list[str] = []
        self.fail = fail

    async def download_file(
        self,
        file_id: str,
        destination: Path,
        *,
        max_bytes: int,
    ) -> Path:
        if self.fail:
            raise TelegramMediaDownloadError("Telegram is unavailable")
        assert max_bytes > 0
        self.file_ids.append(file_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"telegram-media")
        return destination


async def _materialize(
    payload: dict[str, Any],
    client: _MediaClient,
    *,
    media_dir: Path,
    max_bytes: int = 1_000_000,
) -> dict[str, Any]:
    return await materialize_attachments(
        payload,
        cast(HttpTelegramClient, client),
        media_dir=media_dir,
        max_bytes=max_bytes,
    )


def test_telegram_update_maps_a_text_message_to_a_payload() -> None:
    parsed = telegram_event_from_update(_update(update_id=11), allowed_senders=ALLOWED)

    assert parsed is not None
    event_type, payload = parsed
    assert event_type == TELEGRAM_MESSAGE_RECEIVED
    assert payload == {
        "update_id": 11,
        "message_id": 7,
        "chat_id": 999,
        "chat_type": "private",
        "from_id": 4242,
        "from_username": "remi",
        "text": "hello",
        "date": 1_700_000_000,
    }
    Draft202012Validator(telegram_message_received_schema()).validate(payload)


@pytest.mark.parametrize(
    ("media_type", "media", "expected_attachment"),
    [
        (
            "voice",
            {
                "file_id": "voice-file",
                "file_unique_id": "voice-unique",
                "mime_type": "audio/ogg",
                "duration": 5,
                "file_size": 500,
            },
            {
                "type": "voice",
                "file_id": "voice-file",
                "file_unique_id": "voice-unique",
                "mime_type": "audio/ogg",
                "duration": 5,
                "file_size": 500,
            },
        ),
        (
            "photo",
            [
                {"file_id": "photo-small", "width": 32, "height": 32},
                {
                    "file_id": "photo-large",
                    "file_unique_id": "photo-unique",
                    "width": 640,
                    "height": 480,
                    "file_size": 8_000,
                },
            ],
            {
                "type": "photo",
                "file_id": "photo-large",
                "file_unique_id": "photo-unique",
                "width": 640,
                "height": 480,
                "file_size": 8_000,
            },
        ),
        (
            "document",
            {
                "file_id": "document-file",
                "file_unique_id": "document-unique",
                "file_name": "notes.pdf",
                "mime_type": "application/pdf",
                "file_size": 12_000,
            },
            {
                "type": "document",
                "file_id": "document-file",
                "file_unique_id": "document-unique",
                "file_name": "notes.pdf",
                "mime_type": "application/pdf",
                "file_size": 12_000,
            },
        ),
        (
            "audio",
            {
                "file_id": "audio-file",
                "file_unique_id": "audio-unique",
                "file_name": "song.mp3",
                "mime_type": "audio/mpeg",
                "duration": 180,
                "file_size": 1_000_000,
            },
            {
                "type": "audio",
                "file_id": "audio-file",
                "file_unique_id": "audio-unique",
                "file_name": "song.mp3",
                "mime_type": "audio/mpeg",
                "duration": 180,
                "file_size": 1_000_000,
            },
        ),
    ],
)
def test_telegram_update_accepts_supported_media(
    media_type: str,
    media: Any,
    expected_attachment: dict[str, Any],
) -> None:
    parsed = telegram_event_from_update(
        _media_update(media_type, media, caption="Look"),
        allowed_senders=ALLOWED,
    )

    assert parsed is not None
    event_type, payload = parsed
    assert event_type == TELEGRAM_MESSAGE_RECEIVED
    assert payload["text"] == "Look"
    assert payload["attachments"] == [expected_attachment]
    Draft202012Validator(telegram_message_received_schema()).validate(payload)


def test_telegram_update_accepts_media_without_a_caption() -> None:
    parsed = telegram_event_from_update(
        _media_update("voice", {"file_id": "voice-file"}),
        allowed_senders=ALLOWED,
    )

    assert parsed is not None
    _, payload = parsed
    assert payload["text"] == ""
    assert payload["attachments"] == [{"type": "voice", "file_id": "voice-file"}]


@pytest.mark.parametrize(
    ("media_type", "media", "expected_name"),
    [
        ("voice", {"file_id": "voice-file"}, "0-voice.ogg"),
        (
            "photo",
            [{"file_id": "photo-file", "width": 640, "height": 480}],
            "0-photo.jpg",
        ),
        (
            "document",
            {"file_id": "document-file", "file_name": "notes.pdf"},
            "0-document.pdf",
        ),
        (
            "audio",
            {"file_id": "audio-file", "file_name": "song.mp3"},
            "0-audio.mp3",
        ),
    ],
)
async def test_telegram_materializes_supported_media(
    tmp_path: Path,
    media_type: str,
    media: Any,
    expected_name: str,
) -> None:
    client = _MediaClient()
    media_dir = tmp_path / "media"
    parsed = telegram_event_from_update(
        _media_update(media_type, media, update_id=55),
        allowed_senders=ALLOWED,
    )
    assert parsed is not None

    payload = await _materialize(parsed[1], client, media_dir=media_dir)

    attachment = payload["attachments"][0]
    assert attachment["path"] == str(media_dir.resolve() / "55" / expected_name)
    assert Path(attachment["path"]).read_bytes() == b"telegram-media"
    assert client.file_ids == [attachment["file_id"]]
    Draft202012Validator(telegram_message_received_schema()).validate(payload)


async def test_telegram_materialize_reuses_media_on_retry(tmp_path: Path) -> None:
    client = _MediaClient()
    media_dir = tmp_path / "media"
    parsed = telegram_event_from_update(
        _media_update("voice", {"file_id": "voice-file"}, update_id=56),
        allowed_senders=ALLOWED,
    )
    assert parsed is not None

    first = await _materialize(parsed[1], client, media_dir=media_dir)
    again = await _materialize(parsed[1], client, media_dir=media_dir)

    assert first["attachments"] == again["attachments"]
    assert client.file_ids == ["voice-file"]


async def test_telegram_materialize_propagates_a_download_failure(
    tmp_path: Path,
) -> None:
    parsed = telegram_event_from_update(
        _media_update("voice", {"file_id": "voice-file"}),
        allowed_senders=ALLOWED,
    )
    assert parsed is not None

    with pytest.raises(TelegramMediaDownloadError, match="unavailable"):
        await _materialize(parsed[1], _MediaClient(fail=True), media_dir=tmp_path)


async def test_telegram_materialize_rejects_an_attachment_above_the_limit(
    tmp_path: Path,
) -> None:
    client = _MediaClient()
    parsed = telegram_event_from_update(
        _media_update("voice", {"file_id": "voice-file", "file_size": 6}),
        allowed_senders=ALLOWED,
    )
    assert parsed is not None

    with pytest.raises(TelegramMediaTooLargeError):
        await _materialize(parsed[1], client, media_dir=tmp_path, max_bytes=5)
    assert client.file_ids == []


def test_telegram_update_maps_a_reaction_to_a_payload() -> None:
    parsed = telegram_event_from_update(
        _reaction_update(update_id=12), allowed_senders=ALLOWED
    )

    assert parsed is not None
    event_type, payload = parsed
    assert event_type == TELEGRAM_MESSAGE_REACTION
    assert payload == {
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


def test_telegram_reaction_payload_matches_its_schema() -> None:
    parsed = telegram_event_from_update(
        _reaction_update(
            old_reaction=[{"type": "emoji", "emoji": "👍"}],
            new_reaction=[{"type": "custom_emoji", "custom_emoji_id": "emoji-1"}],
        ),
        allowed_senders=ALLOWED,
    )

    assert parsed is not None
    Draft202012Validator(telegram_message_reaction_schema()).validate(parsed[1])


def test_telegram_update_accepts_a_reaction_removal() -> None:
    parsed = telegram_event_from_update(
        _reaction_update(
            old_reaction=[{"type": "emoji", "emoji": "✅"}],
            new_reaction=[],
        ),
        allowed_senders=ALLOWED,
    )

    assert parsed is not None
    _, payload = parsed
    assert payload["old_reaction"] == [{"type": "emoji", "emoji": "✅"}]
    assert payload["new_reaction"] == []


def test_telegram_update_drops_a_sender_that_is_not_allowed() -> None:
    parsed = telegram_event_from_update(_update(from_id=1), allowed_senders=ALLOWED)

    assert parsed is None


def test_telegram_update_drops_a_reaction_from_a_sender_that_is_not_allowed() -> None:
    assert (
        telegram_event_from_update(_reaction_update(from_id=1), allowed_senders=ALLOWED)
        is None
    )


def test_telegram_update_drops_an_anonymous_reaction() -> None:
    update = _reaction_update()
    del update["message_reaction"]["user"]
    update["message_reaction"]["actor_chat"] = {"id": -100, "type": "supergroup"}

    assert telegram_event_from_update(update, allowed_senders=ALLOWED) is None


def test_telegram_update_drops_a_message_without_text() -> None:
    assert (
        telegram_event_from_update(_update(text=None), allowed_senders=ALLOWED) is None
    )


def test_telegram_allowlist_is_empty_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TELEGRAM_ALLOWED_SENDERS", raising=False)

    assert allowed_senders_from_env() == frozenset()


def test_telegram_allowlist_parses_a_comma_separated_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_SENDERS", "1, 2 ,3")

    assert allowed_senders_from_env() == frozenset({1, 2, 3})


def test_telegram_allowlist_rejects_a_non_numeric_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_SENDERS", "1,nobody")

    with pytest.raises(SystemExit, match="non-numeric"):
        allowed_senders_from_env()


def test_telegram_media_limit_defaults_and_validates_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TELEGRAM_MEDIA_MAX_BYTES", raising=False)
    assert telegram_media_max_bytes_from_env() == 20 * 1024 * 1024

    monkeypatch.setenv("TELEGRAM_MEDIA_MAX_BYTES", "0")
    with pytest.raises(SystemExit, match="positive"):
        telegram_media_max_bytes_from_env()


def test_telegram_split_keeps_a_short_message_whole() -> None:
    assert split_message("hello") == ["hello"]


def test_telegram_split_breaks_on_a_line_boundary() -> None:
    text = "a" * 4000 + "\n" + "b" * 500

    parts = split_message(text)

    assert parts == ["a" * 4000, "b" * 500]
    assert all(len(part) <= MAX_MESSAGE_CHARS for part in parts)


def test_telegram_split_hard_splits_text_without_a_boundary() -> None:
    text = "x" * (MAX_MESSAGE_CHARS + 10)

    parts = split_message(text)

    assert len(parts) == 2
    assert all(len(part) <= MAX_MESSAGE_CHARS for part in parts)
    assert "".join(parts) == text


class _RecordingTelegramClient:
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


async def _send_telegram(
    client: _RecordingTelegramClient,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return await send_telegram_message(cast(HttpTelegramClient, client), payload)


async def test_telegram_send_delivers_one_message() -> None:
    client = _RecordingTelegramClient()

    result = await _send_telegram(client, {"chat_id": 999, "text": "hi"})

    assert len(client.calls) == 1
    assert client.calls[0]["chat_id"] == 999
    assert client.calls[0]["text"] == "hi"
    assert result == {"message_ids": [101]}


async def test_telegram_send_threads_only_the_first_part() -> None:
    client = _RecordingTelegramClient()
    text = "a" * 4000 + "\n" + "b" * 500

    await _send_telegram(
        client, {"chat_id": 999, "text": text, "reply_to_message_id": 7}
    )

    assert len(client.calls) == 2
    assert client.calls[0]["reply_to_message_id"] == 7
    assert client.calls[1]["reply_to_message_id"] is None


async def test_telegram_send_defaults_to_plain_text() -> None:
    client = _RecordingTelegramClient()

    await _send_telegram(client, {"chat_id": 999, "text": "*not markdown*"})

    assert client.calls[0]["parse_mode"] is None


async def test_telegram_send_raises_without_a_chat_id() -> None:
    with pytest.raises(RuntimeError, match="requires chat_id and text"):
        await _send_telegram(_RecordingTelegramClient(), {"text": "hi"})


async def test_telegram_send_propagates_a_transport_failure() -> None:
    with pytest.raises(RuntimeError, match="chat not found"):
        await _send_telegram(
            _RecordingTelegramClient(fail=True), {"chat_id": 999, "text": "hi"}
        )


def test_telegram_offset_ledger_starts_without_an_offset() -> None:
    assert OffsetLedger().next_offset is None


def test_telegram_offset_ledger_blocks_on_an_unacked_update() -> None:
    ledger = OffsetLedger()

    ledger.observe(1, needs_ack=True)
    assert ledger.next_offset is None

    ledger.acked(1)
    assert ledger.next_offset == 2


def test_telegram_offset_ledger_confirms_an_ignored_update_immediately() -> None:
    ledger = OffsetLedger()

    ledger.observe(1, needs_ack=False)

    assert ledger.next_offset == 2


def test_telegram_offset_ledger_holds_ignored_updates_behind_unacked_ones() -> None:
    ledger = OffsetLedger()
    ledger.observe(1, needs_ack=True)
    ledger.observe(2, needs_ack=False)

    assert ledger.next_offset is None

    ledger.acked(1)
    assert ledger.next_offset == 3


def test_telegram_offset_ledger_advances_only_the_contiguous_prefix() -> None:
    ledger = OffsetLedger()
    for update_id in (1, 2, 3):
        ledger.observe(update_id, needs_ack=True)

    ledger.acked(2)
    assert ledger.next_offset is None

    ledger.acked(1)
    assert ledger.next_offset == 3

    ledger.acked(3)
    assert ledger.next_offset == 4


def test_telegram_describe_reports_the_manifest() -> None:
    manifest = _describe("telegram")

    assert manifest["id"] == "telegram"
    assert set(manifest["events"]) == {
        TELEGRAM_MESSAGE_RECEIVED,
        TELEGRAM_MESSAGE_REACTION,
        TELEGRAM_MESSAGE_SEND,
    }
    assert manifest["filters"] == {}
    assert [
        (operation["name"], operation["semantics"])
        for operation in manifest["operations"]
    ] == [(TELEGRAM_MESSAGE_SEND, "at_least_once")]


# --- slack ------------------------------------------------------------------


def _slack_callback(
    channel: str = "C123",
    *,
    event_type: str = "message",
    thread_ts: str | None = "40.000",
    **event_overrides: Any,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": event_type,
        "channel": channel,
        "ts": "42.000",
        "user": "U1",
        "text": "hello",
        **event_overrides,
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    return {
        "type": "event_callback",
        "event_id": "Ev1",
        "team_id": "T1",
        "event": event,
    }


class _FakeSlackClient:
    def __init__(self, post_result: dict[str, Any] | None = None) -> None:
        self.post_calls: list[dict[str, Any]] = []
        self.post_result = post_result or {"channel": "C123", "ts": "43.000"}

    async def post_message(
        self,
        channel_id: str,
        text: str,
        *,
        thread_ts: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self.post_calls.append(
            {
                "channel_id": channel_id,
                "text": text,
                "thread_ts": thread_ts,
                "idempotency_key": idempotency_key,
            }
        )
        return self.post_result


def test_slack_callback_maps_to_payload_and_session_id() -> None:
    parsed = slack_event_from_callback(_slack_callback(), channel_ids=("C123",))

    assert parsed is not None
    payload, session_id = parsed
    assert session_id == "slack:T1:C123:40.000"
    assert payload == {
        "event_id": "Ev1",
        "team_id": "T1",
        "channel_id": "C123",
        "message_ts": "42.000",
        "thread_ts": "40.000",
        "user_id": "U1",
        "text": "hello",
    }


def test_slack_callback_keys_the_session_on_the_message_ts_outside_a_thread() -> None:
    parsed = slack_event_from_callback(
        _slack_callback(thread_ts=None), channel_ids=("C123",)
    )

    assert parsed is not None
    payload, session_id = parsed
    assert session_id == "slack:T1:C123:42.000"
    assert payload["thread_ts"] is None


def test_slack_callback_filters_channels() -> None:
    assert (
        slack_event_from_callback(_slack_callback("C999"), channel_ids=("C123",))
        is None
    )


def test_slack_callback_accepts_any_channel_when_unfiltered() -> None:
    parsed = slack_event_from_callback(_slack_callback("C999"), channel_ids=())

    assert parsed is not None
    assert parsed[0]["channel_id"] == "C999"


def test_slack_callback_ignores_unsupported_event_types() -> None:
    body = _slack_callback(event_type="reaction_added")

    assert slack_event_from_callback(body, channel_ids=("C123",)) is None


def test_slack_callback_ignores_bot_and_subtyped_messages() -> None:
    assert (
        slack_event_from_callback(_slack_callback(bot_id="B1"), channel_ids=("C123",))
        is None
    )
    assert (
        slack_event_from_callback(
            _slack_callback(subtype="message_changed"), channel_ids=("C123",)
        )
        is None
    )


def test_slack_callback_ignores_a_non_event_callback_body() -> None:
    body = {"type": "url_verification", "challenge": "challenge-token"}

    assert slack_event_from_callback(body, channel_ids=()) is None
    assert slack_event_from_callback("not a mapping", channel_ids=()) is None


async def test_slack_post_message_delivers_with_the_effect_key() -> None:
    client = _FakeSlackClient(post_result={"channel": "C123", "ts": "43.000"})

    result = await post_slack_message(
        cast(HttpSlackClient, client),
        {"channel_id": "C123", "text": "hello", "thread_ts": "40.000"},
        {"channel_ids": ["C123"]},
        "idem-1",
    )

    assert client.post_calls == [
        {
            "channel_id": "C123",
            "text": "hello",
            "thread_ts": "40.000",
            "idempotency_key": "idem-1",
        }
    ]
    assert result == {
        "channel_id": "C123",
        "message_ts": "43.000",
        "provider_message_id": "C123:43.000",
    }


async def test_slack_post_message_rejects_a_disallowed_channel() -> None:
    client = _FakeSlackClient()

    with pytest.raises(ProviderError, match="not allowed") as error:
        await post_slack_message(
            cast(HttpSlackClient, client),
            {"channel_id": "C999", "text": "hello"},
            {"channel_ids": ["C123"]},
            "idem-1",
        )

    assert error.value.retryable is False
    assert client.post_calls == []


async def test_slack_post_message_requires_channel_and_text() -> None:
    with pytest.raises(ProviderError, match="requires channel_id and text"):
        await post_slack_message(
            cast(HttpSlackClient, _FakeSlackClient()),
            {"text": "hello"},
            {},
            "idem-1",
        )


def test_slack_describe_reports_the_manifest() -> None:
    manifest = _describe("slack")

    assert manifest["id"] == "slack"
    assert set(manifest["events"]) == {SLACK_MESSAGE_RECEIVED, SLACK_MESSAGE_POST}
    assert manifest["filters"][SLACK_MESSAGE_RECEIVED]["required"] == ["channel_ids"]
    assert [
        (operation["name"], operation["semantics"])
        for operation in manifest["operations"]
    ] == [(SLACK_MESSAGE_POST, "connector_deduplicated")]


# --- pushover ---------------------------------------------------------------


def _pushover_client(
    respond: Any,
    *,
    token: str = "app-token",
    user: str = "user-key",
    device: str | None = "watch",
) -> HttpPushoverClient:
    return HttpPushoverClient(
        token=token,
        user=user,
        device=device,
        transport=httpx.MockTransport(respond),
    )


def _recording_client(
    requests: list[httpx.Request],
    response_json: dict[str, Any],
) -> HttpPushoverClient:
    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response_json)

    return _pushover_client(respond)


async def test_pushover_send_message_forwards_present_fields() -> None:
    requests: list[httpx.Request] = []
    client = _recording_client(
        requests,
        {"status": 1, "request": "message-request", "receipt": "receipt-1"},
    )

    result = await send_pushover_message(
        client,
        {"message": "The build is ready.", "title": "Zeta", "priority": 0, "ttl": 3600},
    )

    assert result == {"request_id": "message-request", "receipt": "receipt-1"}
    assert requests[0].url.path == "/1/messages.json"
    assert parse_qs(requests[0].content.decode(), keep_blank_values=True) == {
        "token": ["app-token"],
        "user": ["user-key"],
        "device": ["watch"],
        "message": ["The build is ready."],
        "title": ["Zeta"],
        "priority": ["0"],
        "ttl": ["3600"],
    }


async def test_pushover_send_message_requires_a_message() -> None:
    client = _pushover_client(
        lambda _request: httpx.Response(200, json={"status": 1, "request": "r"})
    )

    with pytest.raises(ProviderError, match="requires a non-empty message") as error:
        await send_pushover_message(client, {})

    assert error.value.retryable is False


async def test_pushover_glance_preserves_clears_and_zero() -> None:
    requests: list[httpx.Request] = []
    client = _recording_client(requests, {"status": 1, "request": "glance-request"})

    result = await update_pushover_glance(
        client, {"text": "", "count": -1, "percent": 0}
    )

    assert result == {"request_id": "glance-request"}
    assert requests[0].url.path == "/1/glances.json"
    assert parse_qs(requests[0].content.decode(), keep_blank_values=True) == {
        "token": ["app-token"],
        "user": ["user-key"],
        "device": ["watch"],
        "text": [""],
        "count": ["-1"],
        "percent": ["0"],
    }


async def test_pushover_glance_requires_a_field() -> None:
    client = _pushover_client(
        lambda _request: httpx.Response(200, json={"status": 1, "request": "r"})
    )

    with pytest.raises(ProviderError, match="requires at least one field"):
        await update_pushover_glance(client, {})


async def test_pushover_client_reports_provider_errors_without_secrets() -> None:
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

    client = _pushover_client(respond, token=token, user=user, device=None)

    with pytest.raises(RuntimeError) as error:
        await client.update_glance({"text": "hello"})

    assert "invalid token" in str(error.value)
    assert token not in str(error.value)
    assert user not in str(error.value)


async def test_pushover_client_propagates_a_transport_failure() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Pushover is unavailable", request=request)

    client = _pushover_client(fail)

    with pytest.raises(httpx.ConnectError, match="unavailable"):
        await client.send_message({"message": "hello"})


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(200, text="not json"), "invalid JSON"),
        (httpx.Response(200, json=[]), "non-object"),
        (httpx.Response(200, json={"status": 1}), "request ID"),
    ],
)
async def test_pushover_client_rejects_invalid_responses(
    response: httpx.Response,
    message: str,
) -> None:
    client = _pushover_client(lambda _request: response)

    with pytest.raises(RuntimeError, match=message):
        await client.send_message({"message": "hello"})


def test_pushover_client_reads_credentials_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PUSHOVER_API_TOKEN", "app-token")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "user-key")
    monkeypatch.setenv("PUSHOVER_DEVICE", "watch")

    client = pushover_client_from_env()

    assert client.token == "app-token"
    assert client.user == "user-key"
    assert client.device == "watch"


def test_pushover_client_requires_both_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PUSHOVER_API_TOKEN", raising=False)
    monkeypatch.delenv("PUSHOVER_USER_KEY", raising=False)

    with pytest.raises(SystemExit, match="PUSHOVER_API_TOKEN"):
        pushover_client_from_env()

    monkeypatch.setenv("PUSHOVER_API_TOKEN", "app-token")
    with pytest.raises(SystemExit, match="PUSHOVER_USER_KEY"):
        pushover_client_from_env()


def test_pushover_describe_reports_the_manifest() -> None:
    manifest = _describe("pushover")

    assert manifest["id"] == "pushover"
    assert set(manifest["events"]) == {PUSHOVER_MESSAGE_SEND, PUSHOVER_GLANCE_UPDATE}
    assert {
        operation["name"]: operation["semantics"]
        for operation in manifest["operations"]
    } == {
        PUSHOVER_MESSAGE_SEND: "at_least_once",
        PUSHOVER_GLANCE_UPDATE: "unsafe_to_retry",
    }
