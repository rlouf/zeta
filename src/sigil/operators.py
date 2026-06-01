"""Semantic operator parsing for stream-oriented glyph routes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast
from .model import chat_json, chat_text, ensure_server
from .state import append_event, append_jsonl, read_jsonl
from .failure import active_failure_context
from .question import recent_question_context
from .session import recent_turns_context

OperatorBase = Literal["?", ",", "@"]

OPERATOR_NAMES: dict[OperatorBase, str] = {
    "?": "answer",
    ",": "propose",
    "@": "goal",
}

SUPPORTED_OPERATORS = frozenset(OPERATOR_NAMES)
OPERATOR_MAX_DEPTHS: dict[OperatorBase, int] = {
    "?": 2,
    ",": 3,
    "@": 2,
}
MAX_STDIN_CHARS = 120_000
MAX_EVENT_OUTPUT_CHARS = 4000
MAX_TARGET_FILES = 16
MAX_TARGET_FILE_CHARS = 20_000
QUESTION_TRANSCRIPT = "last-question.jsonl"

INSPECT_SYSTEM = (
    "You are a semantic shell operator. Inspect the input stream and answer the "
    "user's prompt directly. Be concise, concrete, and grounded in stdin. "
    "Do not claim to have read files or run commands beyond the provided input."
)

RECOMMEND_SYSTEM = (
    "You are a semantic shell operator. Produce one typed proposal from the "
    "input stream, prompt, current project context, and any last-failure "
    "context. The proposal must be one directly runnable shell command. "
    "Do not execute it or claim to have changed anything."
)

PROPOSAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["command"],
            "description": "The proposal kind. Only shell commands are supported.",
        },
        "body": {
            "type": "string",
            "description": "One concrete shell command.",
        },
        "explanation": {
            "type": "string",
            "description": "Brief reason this is the best next action.",
        },
    },
    "required": ["kind", "body", "explanation"],
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


@dataclass(frozen=True)
class OperatorResult:
    """Model output produced by a semantic operator invocation."""

    output: str
    command: str | None = None
    explanation: str = ""
    stderr: str = ""
    exit_code: int = 0


@dataclass(frozen=True)
class TypedProposal:
    """A model proposal with explicit effect kind."""

    kind: Literal["command"]
    body: str
    explanation: str = ""

    def display(self) -> str:
        """Return terminal-visible proposal text."""
        lines = [self.body]
        if self.explanation:
            lines.append(self.explanation)
        return "\n".join(lines)


def parse_operator_token(token: str) -> tuple[OperatorBase, int]:
    """Parse a repeated semantic operator token.

    Examples:

    ```text
    ?   -> ("?", 1)
    ??  -> ("?", 2)
    ,,, -> (",", 3)
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
        name=OPERATOR_NAMES[base],
        prompt=prompt,
        stdin=stdin,
        mode=mode,
    )


def run_invocation(invocation: OperatorInvocation) -> OperatorResult:
    """Run a semantic operator invocation and return stdout text."""
    if invocation.base == "," and invocation.depth > 1:
        raise RuntimeError(
            f"{invocation.glyph} agent step is handled by the Zeta act runner"
        )
    if not ensure_server():
        raise SystemExit(1)

    system = operator_system_prompt(invocation)
    user = operator_user_prompt(invocation)
    proposal = run_proposal_model(invocation, system, user)
    model_output = (
        proposal.body if proposal is not None else run_model(invocation, system, user)
    )
    output = (
        proposal.display()
        if proposal is not None and invocation.depth == 1
        else model_output
    )
    event = append_event(
        {
            "type": "operator_completed",
            "operator": invocation.to_dict(),
            "output_snippet": output[:MAX_EVENT_OUTPUT_CHARS],
            "glyph": invocation.glyph,
        }
    )
    if invocation.base == "?":
        append_inspect_turns(invocation, output, event)
    return OperatorResult(
        output=output,
        command=proposal.body if proposal is not None else None,
        explanation=proposal.explanation if proposal is not None else "",
    )


def operator_system_prompt(invocation: OperatorInvocation) -> str:
    """Return the system prompt for an operator invocation."""
    base = INSPECT_SYSTEM if invocation.base == "?" else RECOMMEND_SYSTEM
    return f"{base}\n\nDepth: {invocation.depth}. {depth_guidance(invocation)}"


def run_proposal_model(
    invocation: OperatorInvocation,
    system: str,
    user: str,
) -> TypedProposal | None:
    """Run the model for typed comma proposals."""
    if invocation.base == "," and invocation.depth == 1:
        data = chat_json(system, user, PROPOSAL_SCHEMA)
        proposal = proposal_from_json(data, explanation_required=True)
        if not proposal:
            raise RuntimeError(", did not produce a proposal")
        return proposal
    return None


def proposal_from_json(
    data: dict[str, object],
    *,
    explanation_required: bool = False,
) -> TypedProposal | None:
    """Convert structured model output into a typed proposal."""
    kind = str(data.get("kind", "")).strip()
    raw_body = str(data.get("body", ""))
    if kind != "command" or not raw_body.strip():
        return None
    body = raw_body.strip()
    explanation = str(data.get("explanation", "")).strip()
    if explanation_required and not explanation:
        return None
    return TypedProposal(
        kind=cast("Literal['command']", kind),
        body=body,
        explanation=explanation,
    )


def run_model(invocation: OperatorInvocation, system: str, user: str) -> str:
    """Run the model for read-only inspect operators."""
    return chat_text(
        system,
        user,
        max_tokens=max_tokens_for_depth(invocation.depth),
    ).rstrip()


def depth_guidance(invocation: OperatorInvocation) -> str:
    """Return operator-specific guidance for repeated glyph depth."""
    if invocation.base == ",":
        return "Comma means recommend one concrete next action."
    return {
        1: "Use a quick pass.",
        2: "Use a deeper pass and call out important caveats.",
    }.get(invocation.depth, "Use a thorough pass and be explicit about uncertainty.")


def operator_user_prompt(invocation: OperatorInvocation) -> str:
    """Return the user prompt sent to the model."""
    if invocation.base == "?":
        return inspect_user_prompt(invocation)
    return proposal_user_prompt(invocation)


def proposal_user_prompt(invocation: OperatorInvocation) -> str:
    """Return a proposal prompt with stdin, file, and failure context."""
    prompt = invocation.prompt or default_prompt(invocation)
    stdin_text, stdin_label = bounded_stdin(invocation.stdin)
    sections = [
        f"Operator: {invocation.glyph} ({invocation.name})",
        f"Prompt: {prompt}",
        proposal_instruction(),
        f"{stdin_label}:\n{stdin_text}",
    ]
    files = readable_target_files(
        [line.strip() for line in invocation.stdin.splitlines() if line.strip()]
    )
    if files:
        sections.append("Readable target file snapshots:")
        for path, content in files:
            label = str(path)
            if len(content) > MAX_TARGET_FILE_CHARS:
                content = content[:MAX_TARGET_FILE_CHARS]
                label = f"{label} (first {MAX_TARGET_FILE_CHARS} chars)"
            sections.append(f"--- {label}\n{content}")
    if invocation.mode == "interactive":
        turns_section = recent_turns_context()
        if turns_section:
            sections.append(turns_section)
        question_section = recent_question_context()
        if question_section:
            sections.append(question_section)
        sections.append(active_failure_context())
    return "\n\n".join(section for section in sections if section)


def bounded_stdin(stdin: str) -> tuple[str, str]:
    """Return bounded stdin text and a display label."""
    if len(stdin) > MAX_STDIN_CHARS:
        return stdin[-MAX_STDIN_CHARS:], f"stdin (last {MAX_STDIN_CHARS} chars)"
    return stdin, "stdin"


def proposal_instruction() -> str:
    """Return proposal guidance for a comma proposal."""
    return "Return exactly one command proposal. Use kind=command."


def inspect_user_prompt(invocation: OperatorInvocation) -> str:
    """Return an inspect prompt with same-terminal transcript context."""
    sections = [
        f"Operator: {invocation.glyph} ({invocation.name})",
        f"Prompt: {invocation.prompt or default_prompt(invocation)}",
    ]
    turns = inspect_turns()
    if turns:
        transcript = "\n\n".join(
            f"{turn['role']}:\n{turn['content']}" for turn in turns
        )
        sections.append(f"Previous question transcript:\n{transcript}")
    stdin_text, stdin_label = bounded_stdin(invocation.stdin)
    sections.append(f"{stdin_label}:\n{stdin_text}")
    return "\n\n".join(sections)


def inspect_turns() -> list[dict[str, object]]:
    """Load same-session question turns visible to inspect prompts."""
    return [
        turn
        for turn in read_jsonl(QUESTION_TRANSCRIPT)
        if turn.get("role") in {"user", "assistant"} and turn.get("content")
    ]


def append_inspect_turns(
    invocation: OperatorInvocation,
    output: str,
    event: dict[str, object],
) -> None:
    """Record inspect turns for same-terminal continuity."""
    event_id = str(event.get("id") or "")
    prompt = invocation.prompt or default_prompt(invocation)
    if invocation.stdin:
        prompt = f"{prompt}\n\nstdin:\n{invocation.stdin}"
    append_jsonl(
        QUESTION_TRANSCRIPT,
        {
            "role": "user",
            "content": prompt,
            "event_id": event_id,
        },
    )
    if output:
        append_jsonl(
            QUESTION_TRANSCRIPT,
            {
                "role": "assistant",
                "content": output,
                "event_id": event_id,
            },
        )


def default_prompt(invocation: OperatorInvocation) -> str:
    """Return a fallback prompt for bare operator invocations."""
    if invocation.base == "?":
        return "Inspect and summarize the input."
    return "Recommend the best next action."


def readable_target_files(lines: list[str]) -> list[tuple[Path, str]]:
    """Read a bounded set of file paths from stdin target lines."""
    files: list[tuple[Path, str]] = []
    for line in lines[:MAX_TARGET_FILES]:
        path = Path(line)
        try:
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files.append((path, content))
    return files


def max_tokens_for_depth(depth: int) -> int:
    """Scale output budget conservatively with operator repetition."""
    if depth <= 1:
        return 700
    return 1200
