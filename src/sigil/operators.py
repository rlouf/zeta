"""Semantic operator parsing for stream-oriented glyph routes."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

from .patches import apply_patch, last_patch, record_patch_apply, store_patch_preview
from .policy import ExecutionPolicy, PolicyDecision, evaluate_policy
from .qwen import chat_json, chat_text, ensure_server
from .security import create_trust_metadata
from .state import append_event, append_jsonl, read_jsonl
from .tty import confirm_on_tty

OperatorBase = Literal["?", ","]

OPERATOR_NAMES: dict[OperatorBase, str] = {
    "?": "inspect",
    ",": "recommend",
}

SUPPORTED_OPERATORS = frozenset(OPERATOR_NAMES)
MAX_OPERATOR_DEPTH = 3
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
    "context. The proposal may be a shell command or a unified diff patch. "
    "Do not execute or claim to have applied anything."
)

APPLY_SYSTEM = (
    "You are a semantic shell operator. Generate exactly one typed proposal "
    "that Sigil can execute or apply after the appropriate boundary checks. "
    "Use kind=command for one shell command. Use kind=patch for a unified diff. "
    "Do not include Markdown fences, prose, numbering, or explanation in body."
)

PROPOSAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["command", "patch"],
            "description": "Whether the proposal is a shell command or unified diff patch.",
        },
        "body": {
            "type": "string",
            "description": "One concrete shell command or unified diff patch.",
        },
        "explanation": {
            "type": "string",
            "description": "Brief reason this is the best next action.",
        },
    },
    "required": ["kind", "body", "explanation"],
}

EXECUTABLE_PROPOSAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["command", "patch"],
            "description": "Whether the proposal is a shell command or unified diff patch.",
        },
        "body": {
            "type": "string",
            "description": "One directly runnable macOS zsh command or unified diff patch.",
        },
    },
    "required": ["kind", "body"],
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
    """Model output plus the policy decision applied to it."""

    output: str
    decision: PolicyDecision
    command: str | None = None
    stderr: str = ""
    exit_code: int = 0


@dataclass(frozen=True)
class TypedProposal:
    """A model proposal with explicit effect kind."""

    kind: Literal["command", "patch"]
    body: str
    explanation: str = ""

    def display(self) -> str:
        """Return terminal-visible proposal text."""
        if self.explanation:
            return f"{self.body}\n{self.explanation}"
        return self.body


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
    if len(token) > MAX_OPERATOR_DEPTH:
        raise ValueError("operator depth must be 1, 2, or 3")
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
    if invocation.depth == 3:
        raise RuntimeError(
            f"{invocation.glyph} bounded autonomy loop is reserved but not implemented"
        )
    if not ensure_server():
        raise SystemExit(1)

    execution_policy = policy or ExecutionPolicy()
    system = operator_system_prompt(invocation)
    user = operator_user_prompt(invocation)
    proposal = run_proposal_model(invocation, system, user)
    output = (
        proposal.display()
        if proposal is not None
        else run_model(invocation, system, user)
    )
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
    if invocation.base == "?":
        append_inspect_turns(invocation, output, event, security)
    if invocation.base == "," and invocation.depth == 2:
        if proposal is None:
            command = executable_command(output)
            proposal = TypedProposal(kind="command", body=command)
        if execution_policy.dry_run:
            return OperatorResult(
                output=proposal.body,
                decision=decision,
                command=proposal.body if proposal.kind == "command" else None,
            )
        if proposal.kind == "patch":
            stored_patch = store_patch_preview(
                patch_text=proposal.body,
                operator=invocation.to_dict(),
                operator_event=event,
                decision=decision,
                security=security,
            )
            if not confirm_patch_application(proposal.body):
                return OperatorResult(
                    output=proposal.body,
                    decision=decision,
                    stderr="sigil op: patch application declined\n",
                    exit_code=2,
                )
            if stored_patch is None:
                return OperatorResult(
                    output=proposal.body,
                    decision=decision,
                    stderr="sigil op: patch proposal was not a unified diff\n",
                    exit_code=1,
                )
            record = last_patch()
            applied = apply_patch(record)
            record_patch_apply(record, applied)
            if applied.ok:
                return OperatorResult(
                    output=proposal.body,
                    decision=decision,
                    stderr="sigil op: patch applied\n",
                )
            return OperatorResult(
                output=proposal.body,
                decision=decision,
                stderr=applied.stderr or "sigil op: patch apply failed\n",
                exit_code=applied.status or 1,
            )
        command = proposal.body
        if execution_policy.confirm_execution and not confirm_execution(command):
            return OperatorResult(
                output="",
                decision=decision,
                command=command,
                stderr="sigil op: command execution declined\n",
                exit_code=2,
            )
        executed = execute_command(command)
        execute_security = create_trust_metadata(
            glyph=invocation.glyph,
            integrity="local_model",
            capability="exec_boxed",
            taint=["model"],
            inputs=[str(event["id"])],
            input_records=[event],
            fresh_human=True,
        )
        append_event(
            {
                "type": "operator_command_executed",
                "operator": invocation.to_dict(),
                "command": command,
                "status": executed.returncode,
                "stdout_snippet": executed.stdout[:MAX_EVENT_OUTPUT_CHARS],
                "stderr_snippet": executed.stderr[:MAX_EVENT_OUTPUT_CHARS],
                **execute_security,
            }
        )
        return OperatorResult(
            output=executed.stdout.rstrip(),
            decision=decision,
            command=command,
            stderr=executed.stderr,
            exit_code=executed.returncode,
        )
    return OperatorResult(output=output, decision=decision)


def operator_system_prompt(invocation: OperatorInvocation) -> str:
    """Return the system prompt for an operator invocation."""
    if invocation.base == "?":
        base = INSPECT_SYSTEM
    elif invocation.depth == 1:
        base = RECOMMEND_SYSTEM
    else:
        base = APPLY_SYSTEM
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
    if invocation.base == "," and invocation.depth == 2:
        data = chat_json(system, user, EXECUTABLE_PROPOSAL_SCHEMA)
        proposal = proposal_from_json(data)
        if not proposal:
            raise RuntimeError(",, did not produce a proposal to apply or execute")
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
    if kind not in {"command", "patch"} or not raw_body.strip():
        return None
    body = raw_body if kind == "patch" else raw_body.strip()
    explanation = str(data.get("explanation", "")).strip()
    if explanation_required and not explanation:
        return None
    return TypedProposal(
        kind=cast("Literal['command', 'patch']", kind),
        body=body,
        explanation=explanation,
    )


def run_model(invocation: OperatorInvocation, system: str, user: str) -> str:
    """Run the model for read-only inspect operators."""
    if invocation.base == ",":
        proposal = run_proposal_model(invocation, system, user)
        if proposal is not None:
            return proposal.display()
        raise RuntimeError(f"{invocation.glyph} did not produce a proposal")
    return chat_text(
        system,
        user,
        max_tokens=max_tokens_for_depth(invocation.depth),
    ).rstrip()


def depth_guidance(invocation: OperatorInvocation) -> str:
    """Return operator-specific guidance for repeated glyph depth."""
    if invocation.base == ",":
        if invocation.depth == 1:
            return "Comma means recommend one concrete next action."
        if invocation.depth == 2:
            return "Comma depth two means generate exactly one command or patch that Sigil will execute or apply."
        return "Comma depth three is handled by the durable plan stepper."
    return {
        1: "Use a quick pass.",
        2: "Use a deeper pass and call out important caveats.",
        3: "Use a thorough pass and organize the result for follow-up work.",
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
        proposal_instruction(invocation),
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
        sections.append(interactive_failure_context())
    return "\n\n".join(section for section in sections if section)


def bounded_stdin(stdin: str) -> tuple[str, str]:
    """Return bounded stdin text and a display label."""
    if len(stdin) > MAX_STDIN_CHARS:
        return stdin[-MAX_STDIN_CHARS:], f"stdin (last {MAX_STDIN_CHARS} chars)"
    return stdin, "stdin"


def proposal_instruction(invocation: OperatorInvocation) -> str:
    """Return proposal guidance for comma depth."""
    if invocation.depth == 2:
        return (
            "Return exactly one executable/applicable proposal. Use kind=patch "
            "for unified diffs that write files, otherwise kind=command."
        )
    return (
        "Return exactly one proposal. Use kind=patch for unified diffs that "
        "write files, otherwise kind=command."
    )


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
    stdin_text = invocation.stdin
    if len(stdin_text) > MAX_STDIN_CHARS:
        stdin_text = stdin_text[-MAX_STDIN_CHARS:]
        stdin_label = f"stdin (last {MAX_STDIN_CHARS} chars)"
    else:
        stdin_label = "stdin"
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
    security: dict[str, object],
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
            **security,
        },
    )
    if output:
        append_jsonl(
            QUESTION_TRANSCRIPT,
            {
                "role": "assistant",
                "content": output,
                "event_id": event_id,
                **security,
            },
        )


def default_prompt(invocation: OperatorInvocation) -> str:
    """Return a fallback prompt for bare operator invocations."""
    if invocation.base == "?":
        return "Inspect and summarize the input."
    if invocation.base == ",":
        if invocation.depth == 2:
            return "Generate one command or patch proposal to execute or apply."
        if invocation.depth == 3:
            return "Run a bounded autonomy loop."
        return "Recommend the best next action."
    return "Recommend the best next action."


def capability_for_operator(
    invocation: OperatorInvocation,
) -> Literal["read", "propose"]:
    """Return the trust capability for an operator invocation."""
    if invocation.base == "?":
        return "read"
    return "propose"


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


def interactive_failure_context() -> str:
    """Return last-failure context for interactive comma proposals."""
    from .failure import failure_context_prompt, last_failure_or_none

    failure = last_failure_or_none()
    if failure is None:
        return "No failed command is recorded for interactive proposal."
    return "Last failed command context:\n" + failure_context_prompt(failure)


def recent_turns_context(limit: int | None = None) -> str:
    """Return a compact summary of the most recent shell turns, if any."""
    from .session import recent_turns_context as _recent_turns_context

    if limit is None:
        return _recent_turns_context()
    return _recent_turns_context(limit=limit)


def max_tokens_for_depth(depth: int) -> int:
    """Scale output budget conservatively with operator repetition."""
    if depth <= 1:
        return 700
    if depth == 2:
        return 1200
    return 1800


def executable_command(output: str) -> str:
    """Extract the shell command from a command-generation response."""
    lines = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            continue
        if not line or line.startswith("#"):
            continue
        if line.startswith(("diff --git", "---", "+++", "@@")):
            continue
        if line.startswith("$ "):
            line = line[2:].strip()
        lines.append(line)
    if not lines:
        raise RuntimeError("comma execution did not produce a command to execute")
    return lines[0]


def execute_command(command: str) -> subprocess.CompletedProcess[str]:
    """Execute a generated shell command through the user's shell."""
    shell = os.environ.get("SHELL") or "/bin/sh"
    return subprocess.run(
        [shell, "-lc", command],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def confirm_execution(command: str) -> bool:
    """Ask the user to confirm a generated command before execution."""
    print("About to execute:", file=sys.stderr)
    print("", file=sys.stderr)
    print(command, file=sys.stderr)
    print("", file=sys.stderr)
    return confirm_on_tty("Run it? [y/N] ")


def confirm_patch_application(patch: str) -> bool:
    """Ask the user to confirm a generated patch before applying it."""
    print("Generated patch preview:", file=sys.stderr)
    print("", file=sys.stderr)
    print(patch, file=sys.stderr)
    print("", file=sys.stderr)
    return confirm_on_tty("Apply this patch? [y/N] ")
