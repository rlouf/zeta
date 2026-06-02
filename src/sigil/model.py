"""Minimal OpenAI-compatible chat completions client.

Sigil keeps this small to avoid taking a runtime dependency on an SDK for the
few model calls it needs. The default configuration targets a local
OpenAI-compatible endpoint.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from .tty import LOVE, MUTED, RESET

DEFAULT_MODEL_URL = "http://127.0.0.1:8080/v1/chat/completions"
DEFAULT_MODEL_NAME = "local-model"


def model_url() -> str:
    """Return the OpenAI-compatible chat completions endpoint."""
    return os.environ.get("SIGIL_MODEL_URL") or DEFAULT_MODEL_URL


def model_name() -> str:
    """Return the model name sent to the configured endpoint."""
    return os.environ.get("SIGIL_MODEL_NAME") or DEFAULT_MODEL_NAME


def model_path() -> str:
    """Return the optional local model path shown in startup help text."""
    return os.environ.get("SIGIL_MODEL_PATH") or "<path-to-model.gguf>"


def endpoint_reachable(url: str) -> bool:
    """Return whether the configured endpoint accepts TCP connections."""
    parsed = urlparse(url)
    host = parsed.hostname
    if host is None:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def ensure_server() -> bool:
    """Check that the configured OpenAI-compatible endpoint is reachable."""
    url = model_url()
    if endpoint_reachable(url):
        return True
    print("", file=sys.stderr)
    print(
        f"{LOVE}✗ model: no OpenAI-compatible endpoint reachable at {url}{RESET}",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    print(f"{MUTED}  Start a local OpenAI-compatible server:", file=sys.stderr)
    print("      llama-server \\", file=sys.stderr)
    print(f"        -m {model_path()} \\", file=sys.stderr)
    print(
        f"        --alias {model_name()} --host 127.0.0.1 --port 8080 \\",
        file=sys.stderr,
    )
    print(f"        -ngl 99 -c 262144 -fa on --reasoning auto{RESET}", file=sys.stderr)
    print("", file=sys.stderr)
    return False


def request_chat_completion(body: dict[str, Any]) -> dict[str, Any]:
    """POST one chat completions request and return the decoded response."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        model_url(),
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"model request failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("model request failed: response was not a JSON object")
    return payload


def chat_json(system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Request schema-constrained JSON from the configured model endpoint."""
    body = {
        "model": model_name(),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": 640,
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "out", "strict": True, "schema": schema},
        },
    }
    payload = request_chat_completion(body)
    content = payload["choices"][0]["message"]["content"]
    return json.loads(content)


def chat_text(system: str, user: str, *, max_tokens: int = 1200) -> str:
    """Request plain text from the configured model endpoint."""
    body = {
        "model": model_name(),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    payload = request_chat_completion(body)
    return str(payload["choices"][0]["message"]["content"])


def model_endpoint_open() -> bool:
    """Return whether the configured OpenAI-compatible server is listening."""
    return endpoint_reachable(model_url())


def ensure_model_for_zeta() -> bool:
    """Check that the local model endpoint is reachable before invoking Zeta."""
    if model_endpoint_open():
        return True
    print(
        f"zeta: local model endpoint is not reachable at {model_url()}",
        file=sys.stderr,
    )
    print("zeta: start the model server or set SIGIL_MODEL_URL", file=sys.stderr)
    return False
