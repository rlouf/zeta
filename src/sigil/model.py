"""Sigil-facing model diagnostics."""

from __future__ import annotations

import os
import sys

from .tty import LOVE, MUTED, RESET
from .zeta.model import model_endpoint_open, model_name, model_url


def model_path() -> str:
    """Return the optional local model path shown in startup help text."""
    return os.environ.get("ZETA_MODEL_PATH") or "<path-to-model.gguf>"


def ensure_server() -> bool:
    """Check that the configured OpenAI-compatible endpoint is reachable."""
    url = model_url()
    if model_endpoint_open():
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
