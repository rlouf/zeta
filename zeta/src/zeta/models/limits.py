"""Model timeouts and context window discovery.

A run must know how long to wait for output and how many tokens the endpoint
accepts. Both answers come from the environment or from endpoint metadata.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

from zeta.models.endpoint import model_server_root
from zeta.models.profiles import model_name, model_url

_MODEL_CONTEXT_TOKENS_CACHE: dict[tuple[str, str], int] = {}

DEFAULT_MODEL_IDLE_TIMEOUT_SECONDS = 120.0

DEFAULT_MODEL_FIRST_OUTPUT_TIMEOUT_SECONDS = 600.0

MODEL_METADATA_TIMEOUT_SECONDS = 0.5


def stream_timeout_from_env(
    env: Mapping[str, str],
    name: str,
    default: float,
) -> float | None:
    """Parse a stream timeout variable; non-positive values disable it."""
    value = env.get(name)
    if value is None or value.strip() == "":
        return default
    try:
        seconds = float(value)
    except ValueError:
        return default
    if seconds <= 0:
        return None
    return seconds


def model_idle_timeout_from_env(env: Mapping[str, str]) -> float | None:
    """Return the configured client-side model stream idle timeout."""
    return stream_timeout_from_env(
        env,
        "ZETA_MODEL_IDLE_TIMEOUT_SECONDS",
        DEFAULT_MODEL_IDLE_TIMEOUT_SECONDS,
    )


def model_idle_timeout() -> float | None:
    """Return the configured client-side model stream idle timeout."""
    return model_idle_timeout_from_env(os.environ)


def model_first_output_timeout_from_env(env: Mapping[str, str]) -> float | None:
    """Return the configured limit on connect plus time to first chunk."""
    return stream_timeout_from_env(
        env,
        "ZETA_MODEL_FIRST_OUTPUT_TIMEOUT_SECONDS",
        DEFAULT_MODEL_FIRST_OUTPUT_TIMEOUT_SECONDS,
    )


def model_first_output_timeout() -> float | None:
    """Return the configured limit on connect plus time to first chunk."""
    return model_first_output_timeout_from_env(os.environ)


def model_stream_timeout(
    *,
    first_output_timeout: float | None,
    idle_timeout: float | None,
) -> Any:
    """Map model timeout intent onto httpx's explicit timeout fields."""
    import httpx

    return httpx.Timeout(
        timeout=None,
        connect=first_output_timeout,
        write=first_output_timeout,
        pool=first_output_timeout,
        read=idle_timeout,
    )


def model_context_tokens(
    selected_url: str | None = None,
    selected_model: str | None = None,
) -> int | None:
    """Return the configured model context length when the server exposes it."""
    resolved_url = model_url(selected_url)
    resolved_model = model_name(selected_model)
    cache_key = (resolved_url, resolved_model)
    cached = _MODEL_CONTEXT_TOKENS_CACHE.get(cache_key)
    if cached is not None:
        return cached
    for endpoint in ("/props", "/v1/models"):
        payload = request_model_metadata(endpoint, selected_url=selected_url)
        if not isinstance(payload, dict):
            continue
        tokens = context_tokens_from_metadata(payload, selected_model=resolved_model)
        if tokens is not None:
            _MODEL_CONTEXT_TOKENS_CACHE[cache_key] = tokens
            return tokens
    return None


def request_model_metadata(
    path: str,
    *,
    selected_url: str | None = None,
) -> dict[str, Any] | None:
    """Fetch a best-effort JSON document from a model metadata endpoint."""
    url = model_server_root(selected_url).rstrip("/") + "/" + path.lstrip("/")
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(
            req, timeout=MODEL_METADATA_TIMEOUT_SECONDS
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (
        OSError,
        TimeoutError,
        http.client.HTTPException,
        urllib.error.URLError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):
        return None
    if isinstance(payload, dict):
        return payload
    return None


def context_tokens_from_metadata(
    payload: dict[str, Any],
    *,
    selected_model: str | None = None,
) -> int | None:
    """Extract a context length from llama-server style metadata."""
    props_tokens = context_tokens_from_props(payload)
    if props_tokens is not None:
        return props_tokens
    return context_tokens_from_models(payload, selected_model=selected_model)


def context_tokens_from_props(payload: dict[str, Any]) -> int | None:
    settings = payload.get("default_generation_settings")
    if isinstance(settings, dict):
        tokens = positive_int(settings.get("n_ctx"))
        if tokens is not None:
            return tokens
        params = settings.get("params")
        if isinstance(params, dict):
            tokens = positive_int(params.get("n_ctx"))
            if tokens is not None:
                return tokens
    return positive_int(payload.get("n_ctx"))


def context_tokens_from_models(
    payload: dict[str, Any],
    *,
    selected_model: str | None = None,
) -> int | None:
    models = candidate_models(payload)
    if not models:
        return None
    for model in models:
        if selected_model and not model_matches_name(model, selected_model):
            continue
        tokens = context_tokens_from_model_entry(model)
        if tokens is not None:
            return tokens
    return context_tokens_from_model_entry(models[0])


def candidate_models(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("data", "models"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def model_matches_name(model: dict[str, Any], selected_model: str) -> bool:
    names = [
        value
        for value in (model.get("id"), model.get("name"), model.get("model"))
        if isinstance(value, str)
    ]
    aliases = model.get("aliases")
    if isinstance(aliases, list):
        names.extend(alias for alias in aliases if isinstance(alias, str))
    return selected_model in names


def context_tokens_from_model_entry(model: dict[str, Any]) -> int | None:
    for key in ("meta", "details"):
        value = model.get(key)
        if isinstance(value, dict):
            tokens = positive_int(value.get("n_ctx"))
            if tokens is not None:
                return tokens
    tokens = positive_int(model.get("context_length"))
    if tokens is not None:
        return tokens
    top_provider = model.get("top_provider")
    if isinstance(top_provider, dict):
        tokens = positive_int(top_provider.get("context_length"))
        if tokens is not None:
            return tokens
    return positive_int(model.get("n_ctx"))


def positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0:
        return None
    return value
