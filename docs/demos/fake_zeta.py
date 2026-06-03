#!/usr/bin/env python3
"""Small Zeta event-stream shim for deterministic Sigil demos."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time


PARSER = """def parse_value(value: str):
    return int(value) if value.isdigit() else value
"""

TEST = """from src.parser import parse_value


def test_parse_value():
    assert parse_value("42") == 42
"""


def answer_for(argv: list[str]) -> str:
    text = " ".join(argv).lower()
    if "risky" in text or "staged" in text or "committed" in text:
        return "Risk is concentrated in parser behavior. Run the focused parser test before pushing."
    if "why failed" in text or ("failed" in text and "pytest" in text):
        return "The pytest run failed in the parser test path. Use the focused parser test as the recovery check."
    if "safest" in text or "next command" in text:
        return "Run `uv run pytest tests/test_parser.py`, then review the staged diff once more."
    if "git command fail" in text or "push" in text:
        return "The branch has no upstream. Use `git push -u origin demo/sigil-flow` after reviewing."
    if "test first" in text:
        return "Start with `uv run pytest tests/test_parser.py`; it covers the changed parser branch."
    return "The change is small: one parser branch, one focused test, and no broad refactor."


def emit(event: dict[str, object]) -> None:
    print(json.dumps(event), flush=True)


def emit_tool(tool: str, args: dict[str, object]) -> None:
    emit({"type": "tool_execution_start", "toolName": tool, "args": args})
    time.sleep(0.15)
    emit({"type": "tool_execution_end", "toolName": tool})


def emit_text(text: str) -> None:
    for chunk in text.split(" "):
        emit(
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "delta": chunk + " ",
                },
            }
        )
        time.sleep(0.02)


def is_act(argv: list[str]) -> bool:
    text = " ".join(argv).lower()
    tools = ""
    if "--tools" in argv:
        index = argv.index("--tools")
        if index + 1 < len(argv):
            tools = argv[index + 1]
    return ("edit" in tools or "write" in tools) and "bounded sigil edit step" in text


def run_act() -> int:
    emit_tool("read", {"path": "src/parser.py"})
    emit_tool("grep", {"pattern": "parse_value", "path": "tests"})
    Path("src/parser.py").write_text(PARSER, encoding="utf-8")
    emit_tool("edit", {"path": "src/parser.py"})
    Path("tests/test_parser.py").write_text(TEST, encoding="utf-8")
    emit_tool("edit", {"path": "tests/test_parser.py"})
    emit_text(
        "Updated src/parser.py and tests/test_parser.py. Next: run `uv run pytest tests/test_parser.py`."
    )
    return 0


def main(argv: list[str]) -> int:
    if argv[:2] == ["transcript", "append"]:
        sys.stdin.read()
        print(json.dumps({"id": "demo-event"}))
        return 0
    if argv[:2] == ["model", "stream"]:
        request = json.loads(sys.stdin.read() or "{}")
        objective = str(request.get("objective") or request.get("prompt") or "")
        command = "uv run pytest tests/test_parser.py"
        reason = "Run the focused parser test."
        if "diff" in objective or "review" in objective:
            command = "git diff --stat"
            reason = "Review the changed files."
        emit(
            {
                "type": "tool_call",
                "name": "bash",
                "input": {"command": command, "reason": reason},
            }
        )
        return 0
    if argv[:2] == ["tool", "bash"]:
        params = json.loads(sys.stdin.read() or "{}")
        if "--analyze" in argv:
            print(
                json.dumps(
                    {
                        "valid": True,
                        "resolved": True,
                        "effects": [
                            {
                                "kind": "execute",
                                "resource": "process",
                                "target": str(params.get("command") or "").split(" ")[
                                    0
                                ],
                                "certainty": "certain",
                            }
                        ],
                        "diagnostics": [],
                    }
                )
            )
            return 0
        print(
            json.dumps(
                {
                    "ok": True,
                    "handoff": {
                        "type": "shell_prompt",
                        "command": params.get("command") or "git status --short",
                        "reason": params.get("reason") or "Run the proposed command.",
                    },
                }
            )
        )
        return 0
    if is_act(argv):
        return run_act()
    answer = answer_for(argv)
    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
