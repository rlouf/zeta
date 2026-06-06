"""Sigil-facing model diagnostics."""

from __future__ import annotations

import os
import sys

from ..tty import LOVE, MUTED, RESET
from . import model as zeta_model


def model_path() -> str:
    """Return the optional local model path shown in startup help text."""
    return os.environ.get("ZETA_MODEL_PATH") or "<path-to-model.gguf>"


def ensure_server(
    *,
    selected_url: str | None = None,
    selected_model: str | None = None,
) -> bool:
    """Check that the configured OpenAI-compatible endpoint is reachable."""
    url = zeta_model.model_url(selected_url)
    if zeta_model.model_endpoint_open(selected_url):
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
        "        --alias "
        f"{zeta_model.model_name(selected_model)} --host 127.0.0.1 --port 8080 \\",
        file=sys.stderr,
    )
    print(f"        -ngl 99 -c 262144 -fa on --reasoning auto{RESET}", file=sys.stderr)
    print("", file=sys.stderr)
    return False
