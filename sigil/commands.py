from __future__ import annotations

import subprocess
import sys
from typing import Any

from .ansi import LOVE, MUTED, RESET
from .qwen import chat_json, ensure_server
from .state import append_event, read_json, write_json

COMMAND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "commands": {
            "type": "array",
            "minItems": 1,
            "maxItems": 4,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"command": {"type": "string"}, "note": {"type": "string"}},
                "required": ["command", "note"],
            },
        }
    },
    "required": ["commands"],
}

COMMAND_SYSTEM = (
    "You generate commands for macOS zsh with the default BSD userland. "
    "Use only BSD/macOS-compatible syntax - no GNU-specific flags or tools "
    "(e.g. no 'find -printf', no 'sed -i' without a backup suffix, no 'date -d', "
    "no 'readlink -f', prefer 'stat -f' over 'stat -c'). Return 2-4 candidate "
    "commands, best first, each with a terse one-line note. Commands must be "
    "directly runnable."
)


def generate(prompt: str) -> list[dict[str, str]]:
    if not ensure_server():
        raise SystemExit(1)
    print(f"{MUTED}⟳ thinking…{RESET}", end="", file=sys.stderr, flush=True)
    try:
        data = chat_json(COMMAND_SYSTEM, prompt, COMMAND_SCHEMA)
    except Exception:
        print("\r\033[K", end="", file=sys.stderr)
        print(f"{LOVE}✗ request failed{RESET}", file=sys.stderr)
        raise SystemExit(1)
    print("\r\033[K", end="", file=sys.stderr)

    candidates = [
        {"command": str(item.get("command", "")), "note": str(item.get("note", ""))}
        for item in data.get("commands", [])
        if item.get("command")
    ]
    if not candidates:
        print(f"{LOVE}✗ no candidates{RESET}", file=sys.stderr)
        raise SystemExit(1)

    state = {"prompt": prompt, "commands": candidates}
    write_json("last-command.json", state)
    append_event({"type": "command_generated", **state})
    return candidates


def previous() -> tuple[str, list[dict[str, str]]]:
    data = read_json("last-command.json")
    if not data or not data.get("commands"):
        print(f"{LOVE}✗ no previous command suggestions{RESET}", file=sys.stderr)
        raise SystemExit(1)
    return str(data.get("prompt", "")), list(data["commands"])


def select(prompt: str, candidates: list[dict[str, str]]) -> str | None:
    if len(candidates) == 1:
        return candidates[0]["command"]

    try:
        subprocess.run(["fzf", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except Exception:
        return select_numbered(prompt, candidates)

    records = []
    for index, item in enumerate(candidates, start=1):
        command = item["command"].replace("\t", " ")
        note = item.get("note", "").replace("\t", " ")
        records.append(f"{index}\t{command}\t{command}\n{MUTED}  {note}{RESET}\n\0")
    proc = subprocess.run(
        [
            "fzf",
            "--read0",
            "--height=16",
            "--layout=reverse",
            "--border=rounded",
            "--color=current-bg:-1,current-fg:15,gutter:0,pointer:0",
            "--ansi",
            "--prompt=command › ",
            "--pointer= ",
            "--marker=+",
            "--gap=1",
            "--gap-line= ",
            "--delimiter=\t",
            "--with-nth=3",
        ],
        input="".join(records),
        text=True,
        stdout=subprocess.PIPE,
    )
    if proc.returncode != 0:
        print(f"{MUTED}cancelled{RESET}", file=sys.stderr)
        return None
    selected = proc.stdout.split("\t", 2)
    if len(selected) < 2:
        return None
    return selected[1]


def select_numbered(prompt: str, candidates: list[dict[str, str]]) -> str | None:
    print(f"{MUTED}commands for {prompt}{RESET}", file=sys.stderr)
    for index, item in enumerate(candidates, start=1):
        print(f"  {index}  {item['command']}", file=sys.stderr)
        if item.get("note"):
            print(f"     {MUTED}{item['note']}{RESET}", file=sys.stderr)
    print(f"  pick 1-{len(candidates)}  ↵=1  q=cancel › ", end="", file=sys.stderr, flush=True)
    choice = sys.stdin.readline().strip()
    if choice == "q":
        print(f"{MUTED}cancelled{RESET}", file=sys.stderr)
        return None
    if not choice:
        choice = "1"
    if choice.isdigit() and 1 <= int(choice) <= len(candidates):
        return candidates[int(choice) - 1]["command"]
    print(f"{LOVE}invalid choice{RESET}", file=sys.stderr)
    return None

