"""Semantic operator parsing for future stream-oriented glyph routes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal, cast

from .qwen import chat_text, ensure_server
from .security import create_trust_metadata
from .state import append_event

OperatorBase = Literal["?", ",", "^"]

OPERATOR_NAMES: dict[OperatorBase, str] = {
    "?": "inspect",
    ",": "propose",
    "^": "repair",
}

SUPPORTED_OPERATORS = frozenset(OPERATOR_NAMES)
MAX_STDIN_CHARS = 120_000
MAX_EVENT_OUTPUT_CHARS = 4000

INSPECT_SYSTEM = (
    "You are a semantic shell operator. Inspect the input stream and answer the "
    "user's prompt directly. Be concise, concrete, and grounded in stdin. "
    "Do not claim to have read files or run commands beyond the provided input."
)

PROPOSE_SYSTEM = (
    "You are a semantic shell operator. Synthesize or propose the requested "
    "output from the input stream. Write only the useful result for stdout. "
    "Avoid chatty framing unless the prompt asks for explanation."
)


@dataclass(frozen=True)
class OperatorInvocation:
    """Parsed semantic operator invocation metadata."""

    glyph: str
    base: OperatorBase
    depth: int
    name: str
    prompt: str
    stdin: str
    mode: str

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation."""
        return asdict(self)


def parse_operator_token(token: str) -> tuple[OperatorBase, int]:
    """Parse a repeated semantic operator token.

    Examples:

    ```text
    ?   -> ("?", 1)
    ??  -> ("?", 2)
    ^^^ -> ("^", 3)
    ```
    """
    if not token:
        raise ValueError("operator token is required")
    base = token[0]
    if base not in SUPPORTED_OPERATORS:
        raise ValueError(f"unsupported operator: {base}")
    operator = cast(OperatorBase, base)
    if any(char != base for char in token):
        raise ValueError(f"operator token must repeat one glyph: {token}")
    return operator, len(token)


def create_invocation(
    token: str,
    *,
    prompt: str = "",
    stdin: str = "",
    mode: str = "interactive",
) -> OperatorInvocation:
    """Create parsed invocation metadata for the semantic operator runtime."""
    base, depth = parse_operator_token(token)
    return OperatorInvocation(
        glyph=token,
        base=base,
        depth=depth,
        name=OPERATOR_NAMES[base],
        prompt=prompt,
        stdin=stdin,
        mode=mode,
    )


def run_invocation(invocation: OperatorInvocation) -> str:
    """Run a semantic operator invocation and return stdout text."""
    if invocation.base == "^":
        append_event(
            {
                "type": "operator_parsed",
                "operator": invocation.to_dict(),
                **create_trust_metadata(
                    glyph=invocation.glyph,
                    integrity="local_model",
                    capability="propose",
                    taint=["model"],
                    fresh_human=True,
                ),
            }
        )
        return f"{invocation.glyph} {invocation.name} depth={invocation.depth}"

    if not ensure_server():
        raise SystemExit(1)

    system = operator_system_prompt(invocation)
    user = operator_user_prompt(invocation)
    output = chat_text(system, user, max_tokens=max_tokens_for_depth(invocation.depth))
    security = create_trust_metadata(
        glyph=invocation.glyph,
        integrity="local_model",
        capability="read" if invocation.base == "?" else "propose",
        taint=["model"],
        fresh_human=True,
    )
    append_event(
        {
            "type": "operator_completed",
            "operator": invocation.to_dict(),
            "output_snippet": output[:MAX_EVENT_OUTPUT_CHARS],
            **security,
        }
    )
    return output.rstrip()


def operator_system_prompt(invocation: OperatorInvocation) -> str:
    """Return the system prompt for an operator invocation."""
    depth_guidance = {
        1: "Use a quick pass.",
        2: "Use a deeper pass and call out important caveats.",
        3: "Use a thorough pass and organize the result for follow-up work.",
    }.get(invocation.depth, "Use a thorough pass and be explicit about uncertainty.")
    base = INSPECT_SYSTEM if invocation.base == "?" else PROPOSE_SYSTEM
    return f"{base}\n\nDepth: {invocation.depth}. {depth_guidance}"


def operator_user_prompt(invocation: OperatorInvocation) -> str:
    """Return the user prompt sent to the model."""
    stdin_text = invocation.stdin
    if len(stdin_text) > MAX_STDIN_CHARS:
        stdin_text = stdin_text[-MAX_STDIN_CHARS:]
        stdin_label = f"stdin (last {MAX_STDIN_CHARS} chars)"
    else:
        stdin_label = "stdin"
    prompt = invocation.prompt or default_prompt(invocation)
    return "\n\n".join(
        [
            f"Operator: {invocation.glyph} ({invocation.name})",
            f"Prompt: {prompt}",
            f"{stdin_label}:\n{stdin_text}",
        ]
    )


def default_prompt(invocation: OperatorInvocation) -> str:
    """Return a fallback prompt for bare operator invocations."""
    if invocation.base == "?":
        return "Inspect and summarize the input."
    if invocation.base == ",":
        return "Propose a useful result from the input."
    return "Repair the input."


def max_tokens_for_depth(depth: int) -> int:
    """Scale output budget conservatively with operator repetition."""
    if depth <= 1:
        return 700
    if depth == 2:
        return 1200
    return 1800
