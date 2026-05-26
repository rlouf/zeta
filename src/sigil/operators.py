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
from .state import append_event

OperatorBase = Literal["?", ",", "^"]

OPERATOR_NAMES: dict[OperatorBase, str] = {
    "?": "inspect",
    ",": "recommend",
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

RECOMMEND_SYSTEM = (
    "You are a semantic shell operator. Recommend one concrete next action "
    "from the input stream and prompt. Be direct and practical. Do not execute "
    "anything."
)

REPAIR_SYSTEM = (
    "You are a semantic shell repair operator. Generate a visible repair "
    "preview only. Prefer a unified diff when file contents are provided. "
    "If a diff is not possible, output a concrete command or patch plan. "
    "Never claim that you applied changes, and do not include destructive "
    "commands without a safer dry-run or review step."
)

REPAIR_APPLY_SYSTEM = (
    "You are a semantic shell repair operator. Generate exactly one concrete "
    "repair that Sigil can apply after showing it to the user. Prefer a unified "
    "diff when file contents are provided. If a diff is not possible, generate "
    "one directly runnable shell command. Do not include Markdown fences, prose, "
    "or explanation in the repair field."
)

RECOMMENDATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "command": {
            "type": "string",
            "description": "One concrete shell command to recommend to the user.",
        },
        "explanation": {
            "type": "string",
            "description": "Brief reason this is the best next action.",
        },
    },
    "required": ["command", "explanation"],
}

EXECUTABLE_COMMAND_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "command": {
            "type": "string",
            "description": "One directly runnable macOS zsh command.",
        },
    },
    "required": ["command"],
}

REPAIR_RECOMMENDATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "repair": {
            "type": "string",
            "description": "One concrete repair command, patch summary, or patch preview.",
        },
        "explanation": {
            "type": "string",
            "description": "Brief reason this is the best repair action.",
        },
    },
    "required": ["repair", "explanation"],
}

REPAIR_APPLICATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["patch", "command"],
            "description": "Whether repair is a unified diff patch or a shell command.",
        },
        "repair": {
            "type": "string",
            "description": "A unified diff patch or one directly runnable macOS zsh command.",
        },
    },
    "required": ["kind", "repair"],
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
    output = run_model(invocation, system, user)
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
    if invocation.base == "," and invocation.depth >= 2:
        command = executable_command(output)
        if execution_policy.dry_run:
            return OperatorResult(output=command, decision=decision, command=command)
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
    if invocation.base == "^":
        if execution_policy.dry_run:
            return OperatorResult(output=output, decision=decision)
        stored_patch = None
        if invocation.depth >= 2:
            stored_patch = store_patch_preview(
                patch_text=output,
                operator=invocation.to_dict(),
                operator_event=event,
                decision=decision,
                security=security,
            )
        if invocation.depth >= 2 and execution_policy.confirm_repair:
            if not confirm_repair_application(output):
                return OperatorResult(
                    output=output,
                    decision=decision,
                    stderr="sigil op: repair application declined\n",
                    exit_code=2,
                )
            if stored_patch is not None:
                record = last_patch()
                applied = apply_patch(record)
                record_patch_apply(record, applied)
                if applied.ok:
                    return OperatorResult(
                        output=output,
                        decision=decision,
                        stderr="sigil op: patch applied\n",
                    )
                return OperatorResult(
                    output=output,
                    decision=decision,
                    stderr=applied.stderr or "sigil op: patch apply failed\n",
                    exit_code=applied.status or 1,
                )
            command = executable_command(output)
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
                    "type": "operator_repair_command_executed",
                    "operator": invocation.to_dict(),
                    "command": command,
                    "status": executed.returncode,
                    "stdout_snippet": executed.stdout[:MAX_EVENT_OUTPUT_CHARS],
                    "stderr_snippet": executed.stderr[:MAX_EVENT_OUTPUT_CHARS],
                    **execute_security,
                }
            )
            return OperatorResult(
                output=output,
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
    elif invocation.base == "^":
        base = REPAIR_SYSTEM if invocation.depth == 1 else REPAIR_APPLY_SYSTEM
    elif invocation.depth == 1:
        base = RECOMMEND_SYSTEM
    else:
        base = (
            "You are a semantic shell operator. Generate exactly one shell "
            "command to execute for the user's request. Output only the command, "
            "with no Markdown fences, prose, numbering, or explanation."
        )
    return f"{base}\n\nDepth: {invocation.depth}. {depth_guidance(invocation)}"


def run_model(invocation: OperatorInvocation, system: str, user: str) -> str:
    """Run the model with structured outputs for comma operators."""
    if invocation.base == "," and invocation.depth == 1:
        data = chat_json(system, user, RECOMMENDATION_SCHEMA)
        command = str(data.get("command", "")).strip()
        if not command:
            raise RuntimeError(", did not produce a command recommendation")
        explanation = str(data.get("explanation", "")).strip()
        if not explanation:
            raise RuntimeError(", did not produce an explanation")
        return f"{command}\n{explanation}"
    if invocation.base == "," and invocation.depth >= 2:
        data = chat_json(system, user, EXECUTABLE_COMMAND_SCHEMA)
        command = str(data.get("command", "")).strip()
        if not command:
            raise RuntimeError(",, did not produce a command to execute")
        return command
    if invocation.base == "^" and invocation.depth == 1:
        data = chat_json(system, user, REPAIR_RECOMMENDATION_SCHEMA)
        repair = str(data.get("repair", "")).strip()
        if not repair:
            raise RuntimeError("^ did not produce a repair recommendation")
        explanation = str(data.get("explanation", "")).strip()
        if not explanation:
            raise RuntimeError("^ did not produce an explanation")
        return f"{repair}\n{explanation}"
    if invocation.base == "^" and invocation.depth >= 2:
        data = chat_json(system, user, REPAIR_APPLICATION_SCHEMA)
        kind = str(data.get("kind", "")).strip()
        raw_repair = str(data.get("repair", ""))
        repair = raw_repair.strip()
        if not repair:
            raise RuntimeError("^^ did not produce a repair to apply")
        return raw_repair if kind == "patch" else repair
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
        return "Comma depth two or higher means generate a command that Sigil will execute."
    if invocation.base == "^":
        if invocation.depth == 1:
            return "Caret means recommend one concrete repair action."
        return (
            "Caret depth two or higher means generate a concrete repair that "
            "Sigil will preview and apply only after confirmation."
        )
    return {
        1: "Use a quick pass.",
        2: "Use a deeper pass and call out important caveats.",
        3: "Use a thorough pass and organize the result for follow-up work.",
    }.get(invocation.depth, "Use a thorough pass and be explicit about uncertainty.")


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
        if invocation.depth >= 2:
            return "Generate a shell command to execute."
        return "Recommend the best next action."
    if invocation.base == "^" and invocation.depth >= 2:
        return "Generate a concrete repair to preview and apply."
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
        repair_instruction(invocation),
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


def repair_instruction(invocation: OperatorInvocation) -> str:
    """Return application guidance for repair depth."""
    if invocation.depth >= 2:
        return (
            "Return a concrete repair only. Prefer a unified diff. If a diff is "
            "not possible, return one directly runnable shell command. Sigil "
            "will show this preview and ask before applying it."
        )
    return "Return a preview only. Do not apply changes."


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


def confirm_repair_application(repair: str) -> bool:
    """Ask the user to confirm a generated repair before applying it."""
    print("Generated repair preview:", file=sys.stderr)
    print("", file=sys.stderr)
    print(repair, file=sys.stderr)
    print("", file=sys.stderr)
    return confirm_on_tty("Apply this repair? [y/N] ")


def confirm_on_tty(prompt: str) -> bool:
    """Read a yes/no confirmation from the controlling terminal."""
    try:
        with open("/dev/tty", "r+", encoding="utf-8") as tty:
            tty.write(prompt)
            tty.flush()
            answer = tty.readline()
    except OSError:
        return False
    return answer.strip().lower() in {"y", "yes"}
