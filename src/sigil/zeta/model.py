"""OpenAI-compatible chat completions transport for Zeta."""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

DEFAULT_MODEL_URL = "http://127.0.0.1:8080/v1/chat/completions"
DEFAULT_MODEL_NAME = "local-model"


def model_url(selected_url: str | None = None) -> str:
    """Return the OpenAI-compatible chat completions endpoint."""
    if selected_url:
        return selected_url
    return model_url_from_env(os.environ)


def model_name(selected_model: str | None = None) -> str:
    """Return the model name sent to the configured endpoint."""
    if selected_model:
        return selected_model
    return os.environ.get("ZETA_MODEL_NAME") or DEFAULT_MODEL_NAME


def model_url_from_env(env: Mapping[str, str]) -> str:
    """Return the configured model URL from explicit environment values."""
    return env.get("ZETA_MODEL_URL") or DEFAULT_MODEL_URL


def model_endpoint_valid(url: str) -> bool:
    """Return whether a model endpoint URL includes a host."""
    return urlparse(url).hostname is not None


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


def model_endpoint_open(selected_url: str | None = None) -> bool:
    """Return whether the configured OpenAI-compatible server is listening."""
    return endpoint_reachable(model_url(selected_url))


def request_chat_completion(
    body: dict[str, Any],
    *,
    selected_url: str | None = None,
) -> dict[str, Any]:
    """POST one chat completions request and return the decoded response."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        model_url(selected_url),
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


def chat_completion_messages(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] = "auto",
    max_tokens: int = 1200,
    selected_model: str | None = None,
    selected_url: str | None = None,
) -> dict[str, Any]:
    """Request one native OpenAI-compatible chat completion message."""
    body: dict[str, Any] = {
        "model": model_name(selected_model),
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice
    if selected_url is None:
        payload = request_chat_completion(body)
    else:
        payload = request_chat_completion(body, selected_url=selected_url)
    message = payload["choices"][0]["message"]
    if not isinstance(message, dict):
        raise RuntimeError("model request failed: assistant message was invalid")
    return message


def chat_text(
    system: str,
    user: str,
    *,
    max_tokens: int = 1200,
    selected_model: str | None = None,
    selected_url: str | None = None,
) -> str:
    """Request plain text from the configured model endpoint."""
    body = {
        "model": model_name(selected_model),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if selected_url is None:
        payload = request_chat_completion(body)
    else:
        payload = request_chat_completion(body, selected_url=selected_url)
    return str(payload["choices"][0]["message"]["content"])
