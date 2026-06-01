"""Zeta v1 runtime services used by the Sigil shell loop."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal, TextIO, cast

from ..model import chat_json, ensure_server
from ..state import append_jsonl, read_jsonl

TRANSCRIPT = "zeta-transcript.jsonl"
DEFAULT_READ_LIMIT = 20_000
DEFAULT_TAIL_LIMIT = 50
MAX_TOOL_RESULT_CHARS = 12_000

EffectKind = Literal["read", "write", "delete", "execute", "search"]
Resource = Literal["path", "process", "session"]


@dataclass(frozen=True)
class ToolSpec:
    """Metadata for one Zeta tool."""

    name: str
    description: str
    schema: dict[str, Any]
    interactive: bool = False

    def metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "schema": self.schema,
            "security": {
                "analyzer": "self",
                "analysis_schema": "zeta.analysis.v1",
            },
            "interactive": self.interactive,
        }


READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path"],
    "properties": {
        "path": {"type": "string"},
        "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1},
    },
}

GREP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["pattern"],
    "properties": {
        "pattern": {"type": "string"},
        "path": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1},
    },
}

LS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "path": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1},
    },
}

BASH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["command"],
    "properties": {
        "command": {"type": "string"},
        "reason": {"type": "string"},
    },
}

EDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["patch"],
    "properties": {
        "patch": {"type": "string"},
        "reason": {"type": "string"},
    },
}

WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path", "content"],
    "properties": {
        "path": {"type": "string"},
        "content": {"type": "string"},
        "reason": {"type": "string"},
    },
}

TOOL_SPECS: dict[str, ToolSpec] = {
    "read": ToolSpec("read", "Read a UTF-8 text file.", READ_SCHEMA),
    "grep": ToolSpec(
        "grep", "Search text with ripgrep or a Python fallback.", GREP_SCHEMA
    ),
    "ls": ToolSpec("ls", "List directory contents.", LS_SCHEMA),
    "bash": ToolSpec(
        "bash", "Stage a shell command into the user's prompt.", BASH_SCHEMA, True
    ),
    "edit": ToolSpec(
        "edit", "Write a patch artifact and stage git apply.", EDIT_SCHEMA, True
    ),
    "write": ToolSpec(
        "write", "Write content to an artifact and stage cp.", WRITE_SCHEMA, True
    ),
}


def tool_metadata(name: str) -> dict[str, Any]:
    spec = TOOL_SPECS.get(name)
    if spec is None:
        raise KeyError(name)
    return spec.metadata()


def allowed_tool_names(allowed_tools: Iterable[str] | None = None) -> list[str]:
    allowed = set(allowed_tools) if allowed_tools is not None else None
    return [name for name in sorted(TOOL_SPECS) if allowed is None or name in allowed]


def tools_list(allowed_tools: Iterable[str] | None = None) -> dict[str, Any]:
    tools = []
    for name in allowed_tool_names(allowed_tools):
        meta = tool_metadata(name)
        meta["command"] = ["zeta", "tool", name]
        meta["origin"] = "builtin"
        tools.append(meta)
    return {"tools": tools}


def model_tool_descriptors(
    allowed_tools: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Return provider-facing tool descriptors for the model prompt."""
    descriptors = []
    for name in allowed_tool_names(allowed_tools):
        spec = TOOL_SPECS[name]
        descriptors.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.schema,
                },
            }
        )
    return descriptors


def model_action_schema(allowed_tools: Iterable[str] | None = None) -> dict[str, Any]:
    names = allowed_tool_names(allowed_tools)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["type"],
        "oneOf": [
            {
                "required": ["type", "content"],
                "properties": {
                    "type": {"type": "string", "enum": ["final"]},
                    "content": {"type": "string", "minLength": 1},
                },
            },
            {
                "required": ["type", "name", "input"],
                "properties": {
                    "type": {"type": "string", "enum": ["tool_call"]},
                    "name": {"type": "string", "enum": names},
                    "input": {"type": "object", "additionalProperties": True},
                },
            },
        ],
        "properties": {
            "type": {
                "type": "string",
                "enum": ["tool_call", "final"],
            },
            "name": {
                "type": "string",
                "enum": names,
            },
            "input": {
                "type": "object",
                "additionalProperties": True,
            },
            "content": {"type": "string"},
        },
    }


def analysis(
    *,
    valid: bool = True,
    resolved: bool = True,
    effects: list[dict[str, Any]] | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "valid": valid,
        "resolved": resolved,
        "effects": effects or [],
        "diagnostics": diagnostics or [],
    }


def effect(
    kind: EffectKind,
    target: str,
    *,
    resource: Resource = "path",
    certainty: str = "certain",
) -> dict[str, str]:
    return {
        "kind": kind,
        "resource": resource,
        "target": target,
        "certainty": certainty,
    }


def diagnostic(
    code: str, message: str, *, severity: str = "unsupported"
) -> dict[str, str]:
    return {"code": code, "message": message, "severity": severity}


def analyze_tool(name: str, params: dict[str, Any]) -> dict[str, Any]:
    if name == "read":
        return analyze_read(params)
    if name == "grep":
        return analyze_grep(params)
    if name == "ls":
        return analyze_ls(params)
    if name == "bash":
        return analyze_bash(params)
    if name == "edit":
        return analyze_edit(params)
    if name == "write":
        return analyze_write(params)
    return analysis(
        valid=False,
        resolved=False,
        diagnostics=[
            diagnostic("unknown-tool", f"unknown tool: {name}", severity="error")
        ],
    )


def analyze_read(params: dict[str, Any]) -> dict[str, Any]:
    path = str(params.get("path") or "")
    if not path:
        return missing("path")
    return analysis(effects=[effect("read", path)])


def analyze_grep(params: dict[str, Any]) -> dict[str, Any]:
    path = str(params.get("path") or ".")
    pattern = str(params.get("pattern") or "")
    if not pattern:
        return missing("pattern")
    return analysis(effects=[effect("search", path)])


def analyze_ls(params: dict[str, Any]) -> dict[str, Any]:
    path = str(params.get("path") or ".")
    return analysis(effects=[effect("read", path)])


SHELL_META_PATTERN = re.compile(r"[|&;<>()`$*?{}\[\]~]")


def analyze_bash(params: dict[str, Any]) -> dict[str, Any]:
    command = str(params.get("command") or "").strip()
    if not command:
        return missing("command")
    diagnostics = []
    resolved = True
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        argv = []
        resolved = False
        diagnostics.append(diagnostic("shell-parse-error", str(exc)))
    if SHELL_META_PATTERN.search(command):
        resolved = False
        diagnostics.append(
            diagnostic("shell-grammar", "command contains shell grammar")
        )
    target = argv[0] if argv else command
    return analysis(
        resolved=resolved,
        effects=[effect("execute", target, resource="process")],
        diagnostics=diagnostics,
    )


def analyze_edit(params: dict[str, Any]) -> dict[str, Any]:
    patch = str(params.get("patch") or "")
    if not patch:
        return missing("patch")
    paths = patch_paths(patch)
    resolved = bool(paths)
    diagnostics = (
        [] if resolved else [diagnostic("patch-paths", "no patch paths found")]
    )
    return analysis(
        resolved=resolved,
        effects=[effect("write", path) for path in paths],
        diagnostics=diagnostics,
    )


def analyze_write(params: dict[str, Any]) -> dict[str, Any]:
    path = str(params.get("path") or "")
    if not path:
        return missing("path")
    return analysis(effects=[effect("write", path)])


def missing(field: str) -> dict[str, Any]:
    return analysis(
        valid=False,
        resolved=False,
        diagnostics=[diagnostic("missing-field", f"missing {field}", severity="error")],
    )


def run_tool(name: str, params: dict[str, Any]) -> dict[str, Any]:
    if name == "read":
        return run_read(params)
    if name == "grep":
        return run_grep(params)
    if name == "ls":
        return run_ls(params)
    if name == "bash":
        return run_bash(params)
    if name == "edit":
        return run_edit(params)
    if name == "write":
        return run_write(params)
    return error_result("unknown-tool", f"unknown tool: {name}")


def run_read(params: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(params.get("path") or ""))
    offset = int(params.get("offset") or 0)
    limit = int(params.get("limit") or DEFAULT_READ_LIMIT)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return error_result("read-failed", str(exc))
    content = text[offset : offset + limit]
    return {
        "ok": True,
        "content": [{"type": "text", "text": content}],
        "metadata": {"path": str(path), "offset": offset, "limit": limit},
    }


def run_grep(params: dict[str, Any]) -> dict[str, Any]:
    pattern = str(params.get("pattern") or "")
    path = str(params.get("path") or ".")
    limit = int(params.get("limit") or 100)
    if not pattern:
        return error_result("missing-pattern", "missing pattern")
    try:
        proc = subprocess.run(
            ["rg", "--line-number", "--max-count", str(limit), pattern, path],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        text = proc.stdout if proc.returncode in {0, 1} else proc.stderr
    except FileNotFoundError:
        text = grep_fallback(pattern, Path(path), limit)
    return {
        "ok": True,
        "content": [{"type": "text", "text": text[:MAX_TOOL_RESULT_CHARS]}],
        "metadata": {"pattern": pattern, "path": path},
    }


def run_ls(params: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(params.get("path") or "."))
    limit = int(params.get("limit") or 200)
    try:
        entries = sorted(
            path.iterdir(), key=lambda entry: (not entry.is_dir(), entry.name)
        )
    except OSError as exc:
        return error_result("ls-failed", str(exc))
    lines = []
    for entry in entries[:limit]:
        name = entry.name + ("/" if entry.is_dir() else "")
        lines.append(name)
    omitted = max(len(entries) - limit, 0)
    if omitted:
        lines.append(f"... {omitted} more")
    return {
        "ok": True,
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "metadata": {"path": str(path), "limit": limit, "entries": len(entries)},
    }


def grep_fallback(pattern: str, root: Path, limit: int) -> str:
    matches: list[str] = []
    paths = [root] if root.is_file() else root.rglob("*")
    for path in paths:
        if len(matches) >= limit:
            break
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines, start=1):
            if pattern in line:
                matches.append(f"{path}:{index}:{line}")
                if len(matches) >= limit:
                    break
    return "\n".join(matches)


def run_bash(params: dict[str, Any]) -> dict[str, Any]:
    command = str(params.get("command") or "").strip()
    if not command:
        return error_result("missing-command", "missing command")
    return handoff(command, str(params.get("reason") or "Run the proposed command."))


def run_edit(params: dict[str, Any]) -> dict[str, Any]:
    patch = str(params.get("patch") or "")
    if not patch:
        return error_result("missing-patch", "missing patch")
    path = write_temp("zeta-edit-", ".patch", patch)
    return handoff(
        f"git apply {shlex.quote(str(path))}",
        str(params.get("reason") or "Apply the staged patch."),
        artifact=str(path),
    )


def run_write(params: dict[str, Any]) -> dict[str, Any]:
    dest = str(params.get("path") or "")
    if not dest:
        return error_result("missing-path", "missing path")
    content = str(params.get("content") or "")
    path = write_temp("zeta-write-", ".tmp", content)
    return handoff(
        f"cp {shlex.quote(str(path))} {shlex.quote(dest)}",
        str(params.get("reason") or f"Write {dest}."),
        artifact=str(path),
    )


def write_temp(prefix: str, suffix: str, content: str) -> Path:
    fd, raw_path = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    path = Path(raw_path)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


def handoff(
    command: str, reason: str, *, artifact: str | None = None
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "type": "shell_prompt",
        "command": command,
        "reason": reason,
    }
    if artifact is not None:
        data["artifact"] = artifact
    return {"ok": True, "handoff": data}


def error_result(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def patch_paths(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        path = patch_path_from_line(line)
        if path and path not in paths:
            paths.append(path)
    return paths


def patch_path_from_line(line: str) -> str | None:
    if line.startswith("+++ "):
        raw = line[4:].strip().split("\t", 1)[0]
    elif line.startswith("--- "):
        raw = line[4:].strip().split("\t", 1)[0]
    else:
        return None
    if raw == "/dev/null":
        return None
    if raw.startswith("a/") or raw.startswith("b/"):
        return raw[2:]
    return raw


def append_transcript(event: dict[str, Any]) -> dict[str, Any]:
    return append_jsonl(TRANSCRIPT, event)


def transcript_tail(limit: int = DEFAULT_TAIL_LIMIT) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    return read_jsonl(TRANSCRIPT)[-limit:]


def zeta_system_prompt() -> str:
    return (
        "You are Zeta, a shell-native agent loop controlled by Sigil's zsh "
        "runtime. Use read and grep for inspection. Use bash, edit, or write "
        "when you need a user-mediated mutation or command handoff. Return one "
        "tool call at a time, or final when done. Keep actions small."
    )


def zeta_user_prompt(
    objective: str,
    transcript: list[dict[str, Any]],
    *,
    allowed_tools: Iterable[str] | None = None,
) -> str:
    return "\n\n".join(
        [
            f"Objective:\n{objective}",
            f"cwd:\n{os.getcwd()}",
            f"Recent transcript JSON:\n{json.dumps(transcript[-20:], ensure_ascii=False)}",
            "Available tools with input JSON Schemas, in provider tool descriptor "
            f"shape:\n{json.dumps(model_tool_descriptors(allowed_tools), ensure_ascii=False)}",
        ]
    )


def next_model_action(
    objective: str,
    transcript: list[dict[str, Any]],
    *,
    system: str | None = None,
    allowed_tools: Iterable[str] | None = None,
) -> dict[str, Any]:
    if not ensure_server():
        raise RuntimeError("model endpoint is not reachable")
    allowed = set(allowed_tools) if allowed_tools is not None else None
    data = chat_json(
        system or zeta_system_prompt(),
        zeta_user_prompt(objective, transcript, allowed_tools=allowed),
        model_action_schema(allowed),
    )
    action_type = str(data.get("type") or "")
    if action_type == "final":
        return {"type": "final", "content": str(data.get("content") or "")}
    name = str(data.get("name") or "")
    raw_input = data.get("input")
    if (
        name not in TOOL_SPECS
        or (allowed is not None and name not in allowed)
        or not isinstance(raw_input, dict)
    ):
        return {
            "type": "final",
            "content": "I could not choose a valid Zeta tool for the next step.",
        }
    return {"type": "tool_call", "name": name, "input": cast(dict[str, Any], raw_input)}


def stream_model_events(request: dict[str, Any]) -> Iterable[dict[str, Any]]:
    objective = str(request.get("objective") or request.get("prompt") or "")
    transcript = request.get("transcript")
    if not isinstance(transcript, list):
        transcript = transcript_tail()
    action = next_model_action(objective, cast(list[dict[str, Any]], transcript))
    if action["type"] == "final":
        content = str(action.get("content") or "")
        if content:
            yield {"type": "assistant_delta", "text": content}
        yield {"type": "final"}
        return
    yield {
        "type": "tool_call",
        "name": action["name"],
        "input": action["input"],
    }


def read_json_stdin(stdin: TextIO) -> dict[str, Any]:
    raw = stdin.read()
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("expected JSON object")
    return data
