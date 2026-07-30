"""Terminal presentation for model endpoint problems.

These helpers write a startup hint when no model endpoint is reachable. They
are presentation, so they belong with the command line, not the model client.
"""

from __future__ import annotations

import os
import sys

from zeta.models.endpoint import model_endpoint_open
from zeta.models.profiles import model_name, model_url

MUTED = "\033[38;2;110;106;134m"

LOVE = "\033[38;2;235;111;146m"

RESET = "\033[0m"


def should_color(stream: object) -> bool:
    return (
        bool(getattr(stream, "isatty", lambda: False)())
        and "NO_COLOR" not in os.environ
    )


def muted(text: str, *, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{MUTED}{text}{RESET}"


def local_model_path() -> str:
    """Return the optional local model path shown in startup help text."""
    return os.environ.get("ZETA_MODEL_PATH") or "<path-to-model.gguf>"


def ensure_server(
    *,
    selected_url: str | None = None,
    selected_model: str | None = None,
) -> bool:
    """Check that the configured OpenAI-compatible endpoint is reachable."""
    url = model_url(selected_url)
    if model_endpoint_open(selected_url):
        return True
    color = should_color(sys.stderr)
    error_line = f"✗ model: no OpenAI-compatible endpoint reachable at {url}"
    if color:
        error_line = f"{LOVE}{error_line}{RESET}"
    hint_lines = [
        "  Start a local OpenAI-compatible server:",
        "      llama-server \\",
        f"        -m {local_model_path()} \\",
        f"        --alias {model_name(selected_model)} --host 127.0.0.1 --port 8080 \\",
        "        -ngl 99 -c 262144 -fa on --reasoning auto",
    ]
    print("", file=sys.stderr)
    print(error_line, file=sys.stderr)
    print("", file=sys.stderr)
    for hint_line in hint_lines:
        print(muted(hint_line, enabled=color), file=sys.stderr)
    print("", file=sys.stderr)
    return False
