from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

from .ansi import LOVE, MUTED, RESET
from .server import qwen_port_open

DEFAULT_URL = "http://127.0.0.1:8080/v1/chat/completions"
DEFAULT_MODEL = "qwen3.6-27b-q8-local"


def qwen_url() -> str:
    return os.environ.get("QWEN_URL", DEFAULT_URL)


def qwen_model() -> str:
    return os.environ.get("QWEN_MODEL", DEFAULT_MODEL)


def ensure_server() -> bool:
    if qwen_port_open():
        return True
    print("", file=sys.stderr)
    print(f"{LOVE}✗ qwen: no local server reachable at {qwen_url()}{RESET}", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"{MUTED}  Start it with your helper script:", file=sys.stderr)
    print("      ~/.config/pi/run-qwen36-q8.sh &", file=sys.stderr)
    print("", file=sys.stderr)
    print("  ...or launch llama-server yourself:", file=sys.stderr)
    print("      llama-server \\", file=sys.stderr)
    print("        -m <path-to-model.gguf> \\", file=sys.stderr)
    print(f"        --alias {qwen_model()} --host 127.0.0.1 --port 8080 \\", file=sys.stderr)
    print(f"        -ngl 99 -c 262144 -fa on --reasoning auto{RESET}", file=sys.stderr)
    print("", file=sys.stderr)
    return False


def chat_json(system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
    body = {
        "model": qwen_model(),
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
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        qwen_url(),
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"qwen request failed: {exc}") from exc
    content = payload["choices"][0]["message"]["content"]
    return json.loads(content)
