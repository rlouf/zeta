"""ChatGPT OAuth credentials for the Codex Responses backend.

Sigil reuses the Codex CLI's credential store at ``~/.codex/auth.json``
rather than running its own login flow. Reads are cheap; a refresh takes
an exclusive lock on a sidecar file, re-reads the store to pick up a
concurrent refresh, and writes the new tokens back atomically.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import fcntl
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OAUTH_REFRESH_TIMEOUT_SECONDS = 30.0
ACCOUNT_CLAIM = "https://api.openai.com/auth"
EXPIRY_MARGIN_SECONDS = 300.0


@dataclass(frozen=True)
class CodexCredentials:
    access_token: str
    account_id: str


def codex_auth_path() -> Path:
    """Return the Codex CLI credential store path."""
    return Path.home() / ".codex" / "auth.json"


def load_codex_credentials(path: Path | None = None) -> CodexCredentials:
    """Return valid credentials, refreshing the access token if expired."""
    path = path or codex_auth_path()
    tokens = read_auth_tokens(path)
    if not access_token_expired(tokens):
        return credentials_from_tokens(tokens)
    with auth_file_lock(path):
        tokens = read_auth_tokens(path)
        if not access_token_expired(tokens):
            return credentials_from_tokens(tokens)
        refreshed = request_token_refresh(str(tokens.get("refresh_token") or ""))
        tokens = {**tokens, **refreshed}
        write_auth_tokens(path, tokens)
    return credentials_from_tokens(tokens)


def read_auth_tokens(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(
            f"no Codex credentials at {path}; run `codex login` once to create them"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not read {path.name}: {exc}") from exc
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        raise RuntimeError(
            f"{path.name} carries no access token; run `codex login` to refresh it"
        )
    return tokens


def write_auth_tokens(path: Path, tokens: dict[str, Any]) -> None:
    payload = {
        "tokens": tokens,
        "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    temp_path = path.with_name(f"{path.name}.tmp")
    temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temp_path, path)


@contextlib.contextmanager
def auth_file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive sidecar lock so concurrent refreshes serialize."""
    lock_path = path.with_name(f"{path.name}.lock")
    with open(lock_path, "w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def credentials_from_tokens(tokens: dict[str, Any]) -> CodexCredentials:
    access_token = str(tokens.get("access_token") or "")
    claims = jwt_claims(access_token)
    auth_claim = claims.get(ACCOUNT_CLAIM)
    auth_claim = auth_claim if isinstance(auth_claim, dict) else {}
    account_id = str(
        auth_claim.get("chatgpt_account_id") or tokens.get("account_id") or ""
    )
    return CodexCredentials(access_token=access_token, account_id=account_id)


def access_token_expired(tokens: dict[str, Any]) -> bool:
    claims = jwt_claims(str(tokens.get("access_token") or ""))
    expires_at = claims.get("exp")
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        return True
    return time.time() >= float(expires_at) - EXPIRY_MARGIN_SECONDS


def jwt_claims(token: str) -> dict[str, Any]:
    """Decode JWT payload claims without verifying the signature.

    The token is only inspected locally for expiry and the account id;
    the backend remains the authority on validity.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload + padding)
        claims = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def request_token_refresh(refresh_token: str) -> dict[str, Any]:
    """Exchange the refresh token for new tokens at the OAuth endpoint."""
    if not refresh_token:
        raise RuntimeError(
            "token refresh failed: no refresh token; run `codex login` again"
        )
    body = json.dumps(
        {
            "client_id": OAUTH_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": "openid profile email",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=OAUTH_REFRESH_TIMEOUT_SECONDS
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"token refresh failed: {exc}") from exc
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise RuntimeError("token refresh failed: response carried no access token")
    tokens = {"access_token": payload["access_token"]}
    for key in ("refresh_token", "id_token"):
        if payload.get(key):
            tokens[key] = payload[key]
    return tokens
