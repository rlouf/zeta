"""Shared fixtures and helpers for the Zeta test modules."""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from typing import Any, cast

from sigil.zeta import agent as zeta_agent
from sigil.zeta import prompt as zeta_prompt
from sigil.zeta import trace as zeta_trace
from sigil.zeta.models import chat_completions as zeta_model


class TtyBuffer(StringIO):
    def isatty(self) -> bool:
        return True


ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def visible_terminal_text(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r", "")


class FakeStreamingResponse:
    def __init__(self, lines: list[bytes], fp: Any = None) -> None:
        self.lines = lines
        self.closed = False
        self.fp = fp

    def __enter__(self) -> FakeStreamingResponse:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __iter__(self) -> Iterator[bytes]:
        return iter(self.lines)

    def close(self) -> None:
        self.closed = True


class DeltaSink:
    def __init__(self) -> None:
        self.deltas: list[str] = []
        self.reasoning_deltas: list[str] = []

    def content_delta(self, text: str) -> None:
        self.deltas.append(text)

    def reasoning_delta(self, text: str) -> None:
        self.reasoning_deltas.append(text)


def required_stream_sink(
    kwargs: dict[str, object],
) -> zeta_model.ChatCompletionStreamSink:
    stream_sink = kwargs.get("stream_sink")
    assert stream_sink is not None
    return cast(zeta_model.ChatCompletionStreamSink, stream_sink)


def sse_lines(*payloads: dict[str, Any] | str) -> list[bytes]:
    lines: list[bytes] = []
    for payload in payloads:
        data = payload if isinstance(payload, str) else json.dumps(payload)
        lines.append(f"data: {data}\n".encode())
        lines.append(b"\n")
    return lines


def write_models_config(home: Path, text: str) -> Path:
    config_dir = home / ".zeta"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "models.toml"
    path.write_text(text, encoding="utf-8")
    return path


def tool_call_fixture(
    call_id: str = "call-read",
    *,
    name: str = "read",
    path: str = "big.txt",
) -> list[dict[str, Any]]:
    return [
        {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps({"path": path})},
        }
    ]


def tool_result_event(
    call_id: str,
    text: str,
    *,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_call_id": call_id,
        "result": {
            "ok": True,
            "content": [{"type": "text", "text": text}],
            "metadata": metadata,
        },
    }


def tool_result_transcript(
    call_id: str,
    text: str,
    *,
    metadata: dict[str, Any],
    tool_name: str = "read",
) -> list[dict[str, Any]]:
    return [
        {
            "type": "assistant_message",
            "tool_calls": tool_call_fixture(call_id, name=tool_name),
        },
        tool_result_event(call_id, text, metadata=metadata),
    ]


def linked_ids_by_kind(
    store: zeta_trace.Store,
    prompt: zeta_trace.Object,
    kind: str,
) -> list[zeta_trace.ObjectId]:
    matches = []
    for object_id in prompt.links:
        linked = store.get_object(object_id)
        if linked is not None and linked.kind == kind:
            matches.append(object_id)
    return matches


def linked_kinds(store: zeta_trace.Store, prompt: zeta_trace.Object) -> list[str]:
    kinds = []
    for object_id in prompt.links:
        linked = store.get_object(object_id)
        if linked is not None:
            kinds.append(linked.kind)
    return kinds


def event_by_type(
    events: list[dict[str, Any]],
    event_type: str,
) -> dict[str, Any]:
    return next(event for event in events if event.get("type") == event_type)


def read_tool_call_response(target: Path) -> dict[str, Any]:
    return {
        "content": "",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "read",
                    "arguments": json.dumps({"path": str(target)}),
                },
            }
        ],
    }


def read_tool_payload(target: Path) -> dict[str, Any]:
    return {
        "ok": True,
        "content": [{"type": "text", "text": "README"}],
        "metadata": {"path": str(target)},
    }


def assert_structural_trim_payload(
    payload: dict[str, Any],
    *,
    call_id: str,
    metadata: dict[str, Any],
    text_lines: int,
) -> None:
    assert payload["trimmed"] is True
    assert payload["trim_method"] == "structural"
    assert payload["tool_call_id"] == call_id
    assert payload["source_object_id"].startswith("sha256:")
    assert payload["tool_result"]["metadata"] == metadata
    assert payload["tool_result"]["content"][0]["text_lines"] == text_lines


def assert_structural_trim_graph(
    store: zeta_trace.InMemoryStore,
    prepared: zeta_prompt.PreparedPrompt,
    payload: dict[str, Any],
    *,
    metadata: dict[str, Any],
) -> None:
    assert prepared.prompt_object_id is not None
    prompt = store.get_object(prepared.prompt_object_id)
    assert prompt is not None
    compacted_ids = linked_ids_by_kind(store, prompt, "compacted_context")
    assert len(compacted_ids) == 1
    compacted = store.get_object(compacted_ids[0])
    assert compacted is not None
    assert compacted.links == (payload["source_object_id"],)
    source = store.get_object(payload["source_object_id"])
    assert source is not None
    assert source.data["source_event"]["type"] == "tool_result"
    assert source.data["source_event"]["result"]["metadata"] == metadata
    assert store.derivations_for_output(compacted_ids[0])[0].producer == (
        "PromptStructuralTrim:v1"
    )
    closure = store.graph_closure([prepared.prompt_object_id])
    assert payload["source_object_id"] in closure


def assert_task_state_graph(
    store: zeta_trace.InMemoryStore,
    prepared: zeta_prompt.PreparedPrompt,
    *,
    source_count: int,
) -> zeta_trace.Object:
    assert prepared.prompt_object_id is not None
    prompt = store.get_object(prepared.prompt_object_id)
    assert prompt is not None
    task_state_ids = linked_ids_by_kind(store, prompt, "task_state")
    assert len(task_state_ids) == 1
    task_state = store.get_object(task_state_ids[0])
    assert task_state is not None
    assert len(task_state.links) == source_count
    assert store.derivations_for_output(task_state_ids[0])[0].producer == (
        "PromptTaskStateExtractor:v1"
    )
    closure = store.graph_closure([prepared.prompt_object_id])
    assert set(task_state.links).issubset(closure)
    return task_state


def assert_tool_result_derivation_graph(
    store: zeta_trace.InMemoryStore,
    result: zeta_agent.AgentTurnResult,
    call_event: dict[str, Any],
    result_event: dict[str, Any],
) -> None:
    call_object_id = call_event["tool_call_object_id"]
    result_object_id = result_event["tool_result_object_id"]
    assert_tool_call_derivation(store, result, call_object_id)
    assert_tool_result_derivation(store, call_object_id, result_object_id)
    assert_prompt_closure_contains_tool_result(
        store,
        result,
        call_object_id,
        result_object_id,
    )


def assert_tool_call_derivation(
    store: zeta_trace.InMemoryStore,
    result: zeta_agent.AgentTurnResult,
    call_object_id: zeta_trace.ObjectId,
) -> None:
    call_object = store.get_object(call_object_id)
    assert call_object is not None
    assert call_object.kind == "tool_call"
    assert call_object.links == (result.prompt_traces[0].assistant_message_object_id,)
    call_derivation = store.derivations_for_output(call_object_id)[0]
    assert call_derivation.producer == "ToolCallProjection"
    assert call_derivation.input_ids == call_object.links


def assert_tool_result_derivation(
    store: zeta_trace.InMemoryStore,
    call_object_id: zeta_trace.ObjectId,
    result_object_id: zeta_trace.ObjectId,
) -> None:
    result_object = store.get_object(result_object_id)
    assert result_object is not None
    assert result_object.kind == "tool_result"
    assert result_object.links == (call_object_id,)
    result_derivation = store.derivations_for_output(result_object_id)[0]
    assert result_derivation.producer == "ToolExecution"
    assert result_derivation.input_ids == (call_object_id,)


def assert_prompt_closure_contains_tool_result(
    store: zeta_trace.InMemoryStore,
    result: zeta_agent.AgentTurnResult,
    call_object_id: zeta_trace.ObjectId,
    result_object_id: zeta_trace.ObjectId,
) -> None:
    second_prompt_id = result.prompt_traces[1].prompt_object_id
    second_closure = store.graph_closure([second_prompt_id])
    assert call_object_id in second_closure
    assert result_object_id in second_closure


def task_state_fixture(
    *,
    objective: str = "continue the implementation",
) -> dict[str, Any]:
    return {
        "objective": objective,
        "constraints": [{"text": "Do not touch unrelated notes.md"}],
        "decisions": [
            {
                "text": "Use structured outputs for task-state extraction",
                "rationale": "The extracted state should be schema-validated",
            }
        ],
        "open_questions": [],
        "files_touched": [
            {
                "path": "src/sigil/zeta/prompt/transforms.py",
                "operation": "modified",
                "status": "in_progress",
                "notes": "Add task-state extraction transform",
            }
        ],
        "pending_tasks": [{"text": "Run regression tests", "priority": "high"}],
        "failed_attempts": [],
    }


def write_skill(
    root: Path,
    name: str,
    *,
    description: str = "Use this skill.",
    body: str = "Skill body.\n",
    metadata_name: str | None = None,
    disabled: bool = False,
) -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    metadata = [
        "---",
        f"description: {description}",
    ]
    if metadata_name is not None:
        metadata.append(f"name: {metadata_name}")
    if disabled:
        metadata.append("disable-model-invocation: true")
    metadata.append("---")
    (skill / "SKILL.md").write_text(
        "\n".join(metadata) + "\n" + body,
        encoding="utf-8",
    )
    return skill


def big_transcript_components(count: int = 6) -> list[zeta_prompt.PromptComponent]:
    timeline = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"message {index} " + "x" * 400,
        }
        for index in range(count)
    ]
    return zeta_prompt.prompt_components("continue", timeline, allowed_tools=())


class BatchSpyStore(zeta_trace.InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.batches = 0

    @contextmanager
    def batch(self) -> Iterator[None]:
        self.batches += 1
        yield


def fake_jwt(claims: dict[str, Any]) -> str:
    def segment(data: dict[str, Any]) -> str:
        raw = json.dumps(data).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{segment({'alg': 'RS256'})}.{segment(claims)}.signature"


def write_codex_auth_file(
    path: Path,
    *,
    expires_in: float = 3600.0,
    account_claim: str | None = "acct_1",
    account_field: str | None = None,
) -> str:
    claims: dict[str, Any] = {"exp": int(time.time() + expires_in)}
    if account_claim is not None:
        claims["https://api.openai.com/auth"] = {"chatgpt_account_id": account_claim}
    access_token = fake_jwt(claims)
    tokens: dict[str, Any] = {
        "access_token": access_token,
        "refresh_token": "refresh-1",
        "id_token": "id-1",
    }
    if account_field is not None:
        tokens["account_id"] = account_field
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tokens": tokens}), encoding="utf-8")
    return access_token
