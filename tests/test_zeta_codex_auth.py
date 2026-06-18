"""Tests for ChatGPT OAuth credential handling for the Codex backend."""

import json
import time
from pathlib import Path
from typing import Any

import pytest
from _zeta_helpers import fake_jwt
from _zeta_helpers import write_codex_auth_file as write_auth_file

from zeta.models import codex_auth


def test_codex_auth_loads_fresh_credentials_without_refresh(
    tmp_path: Path, monkeypatch
) -> None:
    auth_path = tmp_path / "auth.json"
    access_token = write_auth_file(auth_path)

    def fail_refresh(*args: object, **kwargs: object) -> None:
        raise AssertionError("fresh credentials must not refresh")

    monkeypatch.setattr(codex_auth, "request_token_refresh", fail_refresh)

    credentials = codex_auth.load_codex_credentials(auth_path)

    assert credentials.access_token == access_token
    assert credentials.account_id == "acct_1"


def test_codex_auth_reads_account_id_from_tokens_when_claim_missing(
    tmp_path: Path,
) -> None:
    auth_path = tmp_path / "auth.json"
    write_auth_file(auth_path, account_claim=None, account_field="acct_2")

    credentials = codex_auth.load_codex_credentials(auth_path)

    assert credentials.account_id == "acct_2"


def test_codex_auth_refreshes_expired_token_and_writes_back(
    tmp_path: Path, monkeypatch
) -> None:
    auth_path = tmp_path / "auth.json"
    write_auth_file(auth_path, expires_in=-60.0)
    new_access = fake_jwt(
        {
            "exp": int(time.time() + 3600),
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct_1"},
        }
    )
    captured: dict[str, Any] = {}

    def fake_refresh(refresh_token: str) -> dict[str, Any]:
        captured["refresh_token"] = refresh_token
        return {
            "access_token": new_access,
            "refresh_token": "refresh-2",
            "id_token": "id-2",
        }

    monkeypatch.setattr(codex_auth, "request_token_refresh", fake_refresh)

    credentials = codex_auth.load_codex_credentials(auth_path)

    assert captured["refresh_token"] == "refresh-1"
    assert credentials.access_token == new_access
    saved = json.loads(auth_path.read_text(encoding="utf-8"))
    assert saved["tokens"]["access_token"] == new_access
    assert saved["tokens"]["refresh_token"] == "refresh-2"
    assert "last_refresh" in saved


def test_codex_auth_missing_file_says_how_to_login(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="codex login"):
        codex_auth.load_codex_credentials(tmp_path / "absent.json")


def test_codex_auth_rejects_unreadable_auth_file(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text("not json", encoding="utf-8")

    with pytest.raises(RuntimeError, match="auth.json"):
        codex_auth.load_codex_credentials(auth_path)


def test_codex_auth_refresh_failure_is_reported(tmp_path: Path, monkeypatch) -> None:
    auth_path = tmp_path / "auth.json"
    write_auth_file(auth_path, expires_in=-60.0)

    def failing_refresh(refresh_token: str) -> dict[str, Any]:
        raise RuntimeError("token refresh failed: HTTP 401")

    monkeypatch.setattr(codex_auth, "request_token_refresh", failing_refresh)

    with pytest.raises(RuntimeError, match="refresh failed"):
        codex_auth.load_codex_credentials(auth_path)


def test_codex_auth_rereads_after_lock_when_another_process_refreshed(
    tmp_path: Path, monkeypatch
) -> None:
    auth_path = tmp_path / "auth.json"
    write_auth_file(auth_path, expires_in=-60.0)

    def concurrent_refresh(path: Path) -> None:
        write_auth_file(path, expires_in=3600.0, account_claim="acct_other")

    real_lock = codex_auth.auth_file_lock

    def lock_then_simulate(path: Path):
        concurrent_refresh(auth_path)
        return real_lock(path)

    monkeypatch.setattr(codex_auth, "auth_file_lock", lock_then_simulate)

    def fail_refresh(refresh_token: str) -> dict[str, Any]:
        raise AssertionError("must reuse the concurrently refreshed token")

    monkeypatch.setattr(codex_auth, "request_token_refresh", fail_refresh)

    credentials = codex_auth.load_codex_credentials(auth_path)

    assert credentials.account_id == "acct_other"
