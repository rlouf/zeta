#!/usr/bin/env python3
"""Deterministic OpenAI-compatible server for VHS recordings.

The demos exercise the real Sigil CLI. This server only replaces the model
endpoint so recordings are stable and can run offline.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast


def command_for(text: str) -> str:
    lowered = text.lower()
    if "failed command" in lowered and "pytest" in lowered:
        return "uv run pytest tests/test_parser.py"
    if "prompt: fix" in lowered:
        return "uv run pytest tests/test_parser.py"
    if "10 mb" in lowered or "large" in lowered:
        return "find . -path ./.git -prune -o -type f -size +10M -print"
    if "relevant tests" in lowered or "focused test" in lowered:
        return "uv run pytest tests/test_parser.py"
    if "python tests" in lowered:
        return "find tests -name 'test_*.py' -print"
    if "formatter" in lowered:
        return "uv run ruff format src tests"
    if "modified python" in lowered:
        return "git diff --name-only -- '*.py'"
    return "git status --short"


def answer_for(text: str) -> str:
    lowered = text.lower()
    if "risky" in lowered or "staged" in lowered or "committed" in lowered:
        return "Risk is concentrated in parser behavior. Run the focused parser test before pushing."
    if "why failed" in lowered or ("failed" in lowered and "pytest" in lowered):
        return "The pytest run failed in the parser test path. Use the focused parser test as the recovery check."
    if "test first" in lowered:
        return "Start with `uv run pytest tests/test_parser.py`; it covers the changed parser branch."
    if "skip .git" in lowered:
        return "Skipping .git avoids scanning repository internals that can be large and noisy."
    if "summarize this repository" in lowered:
        return "This is a small parser project with one focused test path."
    return "The change is small and bounded to one parser branch."


def completion_for(body: dict[str, Any]) -> dict[str, Any]:
    messages = body.get("messages") or []
    text = "\n\n".join(str(message.get("content", "")) for message in messages)
    schema = ((body.get("response_format") or {}).get("json_schema") or {}).get(
        "schema"
    ) or {}
    properties = schema.get("properties") or {}

    if "command" in properties and "note" in properties:
        command = command_for(text)
        content = {
            "command": command,
            "note": "Smallest useful next command.",
        }
        return chat_response(json.dumps(content))

    if "steps" in properties:
        content = {
            "steps": [
                {
                    "title": "Inspect the branch",
                    "command": "git status --short",
                    "explanation": "Start from the actual Git state.",
                },
                {
                    "title": "Run focused tests",
                    "command": "uv run pytest tests/test_parser.py",
                    "explanation": "Verify the changed parser behavior.",
                },
                {
                    "title": "Check the diff",
                    "command": "git diff --check",
                    "explanation": "Catch whitespace issues before review.",
                },
            ]
        }
        return chat_response(json.dumps(content))

    if "kind" in properties:
        content = {
            "kind": "command",
            "body": command_for(text),
            "explanation": "Runs only the relevant local check.",
        }
        return chat_response(json.dumps(content))

    if {"type", "content", "name", "input"}.issubset(properties):
        content = {
            "type": "final",
            "content": answer_for(text),
        }
        return chat_response(json.dumps(content))

    if "risky" in text.lower() or "staged" in text.lower():
        return chat_response(
            "Risk is concentrated in parser behavior. Run the focused parser test before pushing."
        )
    return chat_response("The change is small and bounded to one parser branch.")


def chat_response(content: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-sigil-demo",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            payload = completion_for(body)
            data = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:
            data = json.dumps({"error": str(exc)}).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port-file", type=Path, required=True)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    host, port = cast("tuple[str, int]", server.server_address)
    args.port_file.write_text(
        f"http://{host}:{port}/v1/chat/completions", encoding="utf-8"
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
