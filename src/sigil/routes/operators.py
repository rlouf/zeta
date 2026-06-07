"""Semantic operator parsing for stream-oriented glyph routes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal, cast

OperatorBase = Literal[",", "?"]

OPERATOR_NAMES: dict[OperatorBase, str] = {
    ",": "ask",
    "?": "status",
}
COMMA_OPERATOR_NAMES = {
    1: "ask",
    2: "propose",
    3: "do",
}

SUPPORTED_OPERATORS = frozenset(OPERATOR_NAMES)
OPERATOR_MAX_DEPTHS: dict[OperatorBase, int] = {
    ",": 3,
    "?": 1,
}


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
    ,   -> (",", 1)
    ,,, -> (",", 3)
    ?   -> ("?", 1)
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
    max_depth = OPERATOR_MAX_DEPTHS[operator]
    if len(token) > max_depth:
        allowed = " or ".join(str(depth) for depth in range(1, max_depth + 1))
        raise ValueError(f"{base} operator depth must be {allowed}")
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
        name=operator_name(base, depth),
        prompt=prompt,
        stdin=stdin,
        mode=mode,
    )


def operator_name(base: OperatorBase, depth: int) -> str:
    """Return the user-facing verb for a parsed operator."""
    if base == ",":
        return COMMA_OPERATOR_NAMES[depth]
    return OPERATOR_NAMES[base]
