"""Single-command proposal for the comma glyph.

Sigil , asks the local model for one typed proposal: a runnable shell command
with a brief explanation.  Shell bindings only ask it for that proposal.
"""

from __future__ import annotations

import sys
from typing import Any

from .ansi import LOVE, MUTED, RESET
from .model import chat_json, ensure_server
from .security import create_trust_metadata
from .state import append_event

PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "command": {
            "type": "string",
            "description": "One directly runnable macOS zsh command.",
        },
        "note": {
            "type": "string",
            "description": "Brief reason this is the best next action.",
        },
    },
    "required": ["command", "note"],
}

COMMAND_SYSTEM = (
    "You generate commands for macOS zsh with the default BSD userland. "
    "Use only BSD/macOS-compatible syntax - no GNU-specific flags or tools "
    "(e.g. no 'find -printf', no 'sed -i' without a backup suffix, no 'date -d', "
    "no 'readlink -f', prefer 'stat -f' over 'stat -c'). "
    "Return exactly one directly runnable command with a terse one-line note."
)


def generate(prompt: str) -> tuple[dict[str, str], dict[str, Any]]:
    """Ask the local model for a single command proposal."""
    if not ensure_server():
        raise SystemExit(1)
    print(f"{MUTED}❯ sigil ,  · propose · model-authored{RESET}", file=sys.stderr)
    print(f"{MUTED}⟳ thinking…{RESET}", end="", file=sys.stderr, flush=True)
    try:
        data = chat_json(COMMAND_SYSTEM, prompt, PROPOSAL_SCHEMA)
    except RuntimeError as exc:
        print("\r\033[K", end="", file=sys.stderr)
        print(f"{LOVE}✗ model request failed{RESET}", file=sys.stderr)
        print(f"  {exc}", file=sys.stderr)
        print("  Check that the local model server is still running.", file=sys.stderr)
        raise SystemExit(1) from exc
    except Exception:
        print("\r\033[K", end="", file=sys.stderr)
        print(f"{LOVE}✗ could not generate command proposal{RESET}", file=sys.stderr)
        raise SystemExit(1)
    print("\r\033[K", end="", file=sys.stderr)

    command = str(data.get("command", "")).strip()
    if not command:
        print(f"{LOVE}✗ no command generated{RESET}", file=sys.stderr)
        raise SystemExit(1)

    proposal = {"command": command, "note": str(data.get("note", "")).strip()}

    security = create_trust_metadata(
        glyph=",",
        mode="propose",
    )
    event = append_event(
        {
            "type": "command_generated",
            "prompt": prompt,
            "command": command,
            **security,
        }
    )
    selection_security = create_trust_metadata(
        glyph=",",
        mode="propose",
        inputs=[str(event["id"])],
        input_records=[event],
    )
    return proposal, selection_security
