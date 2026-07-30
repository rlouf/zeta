"""Model endpoint reachability.

Zeta talks to an OpenAI-compatible endpoint. These helpers answer whether one
is configured and reachable, without sending a completion request.
"""

from __future__ import annotations

import socket
from urllib.parse import urlparse, urlunparse

from zeta.models.profiles import model_url


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


def model_server_root(selected_url: str | None = None) -> str:
    """Return the endpoint root for sibling metadata endpoints."""
    parsed = urlparse(model_url(selected_url))
    path = parsed.path.rstrip("/")
    for suffix in ("/v1/chat/completions", "/chat/completions"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    else:
        path = ""
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
