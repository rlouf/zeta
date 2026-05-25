"""Semantic operator parsing for future stream-oriented glyph routes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

from .patches import store_patch_preview
from .policy import ExecutionPolicy, PolicyDecision, evaluate_policy
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
MAX_REPAIR_FILES = 16
MAX_REPAIR_FILE_CHARS = 20_000

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

REPAIR_SYSTEM = (
    "You are a semantic shell repair operator. Generate a visible repair "
    "preview only. Prefer a unified diff when file contents are provided. "
    "If a diff is not possible, output a concrete command or patch plan. "
    "Never claim that you applied changes, and do not include destructive "
    "commands without a safer dry-run or review step."
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


@dataclass(frozen=True)
class OperatorResult:
    """Model output plus the policy decision applied to it."""

    output: str
    decision: PolicyDecision


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


def run_invocation(
    invocation: OperatorInvocation,
    *,
    policy: ExecutionPolicy | None = None,
) -> OperatorResult:
    """Run a semantic operator invocation and return stdout text."""
    if not ensure_server():
        raise SystemExit(1)

    execution_policy = policy or ExecutionPolicy()
    system = operator_system_prompt(invocation)
    user = operator_user_prompt(invocation)
    output = chat_text(system, user, max_tokens=max_tokens_for_depth(invocation.depth))
    output = output.rstrip()
    decision = evaluate_policy(
        glyph=invocation.glyph,
        depth=invocation.depth,
        output=output,
        policy=execution_policy,
    )
    security = create_trust_metadata(
        glyph=invocation.glyph,
        integrity="local_model",
        capability=capability_for_operator(invocation),
        taint=["model"],
        fresh_human=True,
    )
    event = append_event(
        {
            "type": "operator_completed",
            "operator": invocation.to_dict(),
            "output_snippet": output[:MAX_EVENT_OUTPUT_CHARS],
            "policy": execution_policy.to_dict(),
            "decision": decision.to_dict(),
            **security,
        }
    )
    if invocation.base == "^":
        store_patch_preview(
            patch_text=output,
            operator=invocation.to_dict(),
            operator_event=event,
            decision=decision,
            security=security,
        )
    return OperatorResult(output=output, decision=decision)


def operator_system_prompt(invocation: OperatorInvocation) -> str:
    """Return the system prompt for an operator invocation."""
    depth_guidance = {
        1: "Use a quick pass.",
        2: "Use a deeper pass and call out important caveats.",
        3: "Use a thorough pass and organize the result for follow-up work.",
    }.get(invocation.depth, "Use a thorough pass and be explicit about uncertainty.")
    if invocation.base == "?":
        base = INSPECT_SYSTEM
    elif invocation.base == "^":
        base = REPAIR_SYSTEM
    else:
        base = PROPOSE_SYSTEM
    return f"{base}\n\nDepth: {invocation.depth}. {depth_guidance}"


def operator_user_prompt(invocation: OperatorInvocation) -> str:
    """Return the user prompt sent to the model."""
    if invocation.base == "^":
        return repair_user_prompt(invocation)
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


def capability_for_operator(
    invocation: OperatorInvocation,
) -> Literal["read", "propose"]:
    """Return the trust capability for an operator invocation."""
    if invocation.base == "?":
        return "read"
    return "propose"


def repair_user_prompt(invocation: OperatorInvocation) -> str:
    """Return a repair prompt with path-aware context when stdin names files."""
    prompt = invocation.prompt or default_prompt(invocation)
    lines = [line.strip() for line in invocation.stdin.splitlines() if line.strip()]
    files = repair_files(lines)
    sections = [
        f"Operator: {invocation.glyph} ({invocation.name})",
        f"Prompt: {prompt}",
        "Return a preview only. Do not apply changes.",
        "stdin targets:\n" + (invocation.stdin if invocation.stdin else "<empty>"),
    ]
    if files:
        sections.append("Readable target file snapshots:")
        for path, content in files:
            label = str(path)
            if len(content) > MAX_REPAIR_FILE_CHARS:
                content = content[:MAX_REPAIR_FILE_CHARS]
                label = f"{label} (first {MAX_REPAIR_FILE_CHARS} chars)"
            sections.append(f"--- {label}\n{content}")
    elif not invocation.stdin and invocation.mode == "interactive":
        sections.append(interactive_failure_context())
    else:
        sections.append("No readable file snapshots were found from stdin.")
    return "\n\n".join(sections)


def repair_files(lines: list[str]) -> list[tuple[Path, str]]:
    """Read a bounded set of file paths from stdin target lines."""
    files: list[tuple[Path, str]] = []
    for line in lines[:MAX_REPAIR_FILES]:
        path = Path(line)
        try:
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files.append((path, content))
    return files


def interactive_failure_context() -> str:
    """Return last-failure context for interactive repair operators."""
    try:
        from .failure import fix_prompt, last_failure

        failure = last_failure()
    except SystemExit:
        return "No failed command is recorded for interactive repair."
    return "Last failed command context:\n" + fix_prompt(failure)


def max_tokens_for_depth(depth: int) -> int:
    """Scale output budget conservatively with operator repetition."""
    if depth <= 1:
        return 700
    if depth == 2:
        return 1200
    return 1800
