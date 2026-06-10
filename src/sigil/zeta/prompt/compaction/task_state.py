"""Structured task-state extraction for prompt compaction."""

from __future__ import annotations

import json
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from ...model import chat_structured_output
from ..components import PromptComponent

TASK_STATE_RESPONSE_NAME = "zeta_task_state"
TASK_STATE_SCHEMA_NAME = "zeta.task_state.v1"

TASK_STATE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "objective",
        "constraints",
        "decisions",
        "open_questions",
        "files_touched",
        "pending_tasks",
        "failed_attempts",
    ],
    "properties": {
        "objective": {"type": "string"},
        "constraints": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
            },
        },
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "rationale"],
                "properties": {
                    "text": {"type": "string"},
                    "rationale": {"type": "string"},
                },
            },
        },
        "open_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
            },
        },
        "files_touched": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "operation", "status", "notes"],
                "properties": {
                    "path": {"type": "string"},
                    "operation": {"type": "string"},
                    "status": {"type": "string"},
                    "notes": {"type": "string"},
                },
            },
        },
        "pending_tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "priority"],
                "properties": {
                    "text": {"type": "string"},
                    "priority": {"type": "string"},
                },
            },
        },
        "failed_attempts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["action", "error", "lesson"],
                "properties": {
                    "action": {"type": "string"},
                    "error": {"type": "string"},
                    "lesson": {"type": "string"},
                },
            },
        },
    },
}

TASK_STATE_SYSTEM_PROMPT = """\
Extract compact task state from prior agent timeline components.

Return only the current state that matters for continuing the task:
- objective: the user's active objective, not implementation chatter
- constraints: explicit requirements, boundaries, preferences, and naming decisions
- decisions: resolved implementation or product choices
- open_questions: unresolved questions or ambiguities
- files_touched: files mentioned as created, modified, inspected, or important
- pending_tasks: concrete remaining work
- failed_attempts: attempts that failed, with the useful lesson

Prefer precise, deduplicated facts. Use empty arrays when a category has no facts.
Do not include generic conversation summary prose.
"""


class TaskStateExtractor(Protocol):
    """Extract structured task state from prompt components."""

    def extract(self, components: list[PromptComponent]) -> dict[str, Any]: ...


class ModelTaskStateExtractor:
    """Use the configured model's structured outputs to extract task state."""

    def __init__(
        self,
        *,
        selected_model: str | None = None,
        selected_url: str | None = None,
        max_tokens: int = 1200,
    ) -> None:
        self.selected_model = selected_model
        self.selected_url = selected_url
        self.max_tokens = max_tokens

    def extract(self, components: list[PromptComponent]) -> dict[str, Any]:
        return chat_structured_output(
            task_state_extraction_messages(components),
            schema=TASK_STATE_SCHEMA,
            response_name=TASK_STATE_RESPONSE_NAME,
            max_tokens=self.max_tokens,
            selected_model=self.selected_model,
            selected_url=self.selected_url,
        )


class TaskStateExtractionPromptTransform:
    """Replace prior timeline messages with structured task state."""

    producer = "PromptTaskStateExtractor:v1"

    def __init__(
        self,
        *,
        extractor: TaskStateExtractor | None = None,
        fail_open: bool = True,
    ) -> None:
        self.extractor = extractor or ModelTaskStateExtractor()
        self.fail_open = fail_open

    def apply(self, components: list[PromptComponent]) -> list[PromptComponent]:
        sources = task_state_source_components(components)
        if not sources:
            return list(components)
        try:
            state = validated_task_state(self.extractor.extract(sources))
        except Exception:
            if self.fail_open:
                return list(components)
            raise
        compacted = task_state_component(state, sources)
        return replace_sources_with_task_state(components, sources, compacted)


def replace_sources_with_task_state(
    components: list[PromptComponent],
    sources: list[PromptComponent],
    task_state: PromptComponent,
) -> list[PromptComponent]:
    source_ids = {id(component) for component in sources}
    output: list[PromptComponent] = []
    inserted = False
    for component in components:
        if id(component) not in source_ids:
            output.append(component)
            continue
        if not inserted:
            output.append(task_state)
            inserted = True
    return output


def task_state_source_components(
    components: list[PromptComponent],
) -> list[PromptComponent]:
    """Return older timeline components that can be replaced by task state."""
    return [
        component
        for component in components
        if component.kind == "transcript_message"
        and component.object_id is not None
        and component.message is not None
    ]


def task_state_component(
    state: dict[str, Any],
    sources: list[PromptComponent],
) -> PromptComponent:
    """Return a prompt component carrying extracted task state."""
    source_ids = tuple(
        component.object_id for component in sources if component.object_id is not None
    )
    message = task_state_message(state)
    return PromptComponent(
        kind="task_state",
        representation="summary",
        data={
            "method": "task_state_extraction",
            "schema": TASK_STATE_SCHEMA_NAME,
            "source_count": len(source_ids),
            "state": state,
            "message": message,
        },
        message=message,
        links=source_ids,
    )


def task_state_message(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "user",
        "content": "Task state JSON:\n" + task_state_json(state),
    }


def task_state_json(state: dict[str, Any]) -> str:
    return json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def task_state_extraction_messages(
    components: list[PromptComponent],
) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": TASK_STATE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "Prior timeline components JSON:\n"
            + json.dumps(
                [
                    component_for_extraction(index, component)
                    for index, component in enumerate(components)
                ],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    ]


def component_for_extraction(
    index: int,
    component: PromptComponent,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "index": index,
        "kind": component.kind,
        "object_id": component.object_id,
        "message": component.message,
        "source_event_type": component.data.get("source_event_type", ""),
        "source_event_role": component.data.get("source_event_role", ""),
    }
    source_event = component.data.get("source_event")
    if isinstance(source_event, dict):
        data["source_event"] = source_event
    return data


def validated_task_state(state: dict[str, Any]) -> dict[str, Any]:
    try:
        Draft202012Validator(TASK_STATE_SCHEMA).validate(state)
    except ValidationError as exc:
        raise RuntimeError(f"task state failed validation: {exc}") from exc
    return state
