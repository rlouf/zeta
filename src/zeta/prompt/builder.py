"""Prompt builder and trace recording for Zeta.

Prompt component order is a public contract for prefix-cache friendliness:
system_prompt, tool descriptors, project context, then volatile components.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import nullcontext
from dataclasses import dataclass, replace
from typing import Any

from ..models import (
    DEFAULT_MAX_COMPLETION_TOKENS,
    ModelOutput,
    chat_completion_request_body,
)
from ..skills import Skill, available_skills
from ..tools.base import content_hash
from ..trace import (
    Derivation,
    Object,
    ObjectId,
    PromptTrace,
    Store,
    canonical_json,
    warn_trace_failure_once,
)
from .components import (
    PromptComponent,
    component_messages,
    prompt_component_object,
    prompt_components,
)
from .system import can_read_skill_files, enabled_capability_ids
from .transforms import NoOpPromptTransform, PromptTransform


@dataclass(frozen=True)
class PreparedPrompt:
    """A model-ready prompt plus trace ids for its stored object graph."""

    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    tool_choice: str | dict[str, Any]
    payload: dict[str, Any]
    prompt_object_id: ObjectId | None = None
    component_object_ids: tuple[ObjectId, ...] = ()


class PromptBuilder:
    """Build model prompts and record their trace object graph."""

    def __init__(
        self,
        *,
        store: Store | None = None,
        transform: PromptTransform | None = None,
    ) -> None:
        self._store = store
        self.transform = transform or NoOpPromptTransform()
        # One builder serves every model call of a turn; skills discovery
        # walks the filesystem, so do it once per tool set instead.
        self._skills: dict[tuple[str, ...], list[Skill]] = {}

    def build(
        self,
        objective: str,
        timeline: list[dict[str, Any]],
        *,
        system: str | None = None,
        allowed_capabilities: Iterable[str] | None = None,
        context: str = "",
        current_events: Iterable[dict[str, Any]] = (),
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] = "auto",
        max_tokens: int = DEFAULT_MAX_COMPLETION_TOKENS,
        selected_model: str | None = None,
        thinking: str | None = None,
    ) -> PreparedPrompt:
        components = prompt_components(
            objective,
            timeline,
            system=system,
            allowed_capabilities=allowed_capabilities,
            context=context,
            current_events=current_events,
            tools=tools,
            skills=self._skills_for(allowed_capabilities),
        )
        fallback_tools = tools or []
        return self._build_traced_prompt(
            components,
            tools=fallback_tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
            selected_model=selected_model,
            thinking=thinking,
        )

    def record_assistant_message(
        self,
        prepared: PreparedPrompt,
        model_output: ModelOutput,
    ) -> PromptTrace | None:
        store = self.store()
        if store is None or prepared.prompt_object_id is None:
            return None
        try:
            assistant_id = store.put_object(
                Object(
                    kind="assistant_message",
                    schema="zeta.model_output.v1",
                    data=model_output.to_trace_data(),
                    links=(prepared.prompt_object_id,),
                )
            )
            store.record_derivation(
                Derivation(
                    producer="ModelResponse",
                    output_id=assistant_id,
                    input_ids=(prepared.prompt_object_id,),
                    params={},
                )
            )
            return PromptTrace(
                prompt_object_id=prepared.prompt_object_id,
                assistant_message_object_id=assistant_id,
            )
        except Exception as exc:
            warn_trace_failure_once("record_assistant_message", exc)
            return None

    def record_tool_call(
        self,
        trace: PromptTrace,
        event: dict[str, Any],
    ) -> ObjectId | None:
        """Record a first-class tool call projected from the model response."""
        store = self.store()
        source_id = trace.assistant_message_object_id or trace.prompt_object_id
        if store is None or not source_id:
            return None
        try:
            call_id = store.put_object(
                Object(
                    kind="tool_call",
                    schema="zeta.tool_call.v1",
                    data=tool_call_object_data(event),
                    links=(source_id,),
                )
            )
            store.record_derivation(
                Derivation(
                    producer="ToolCallProjection",
                    output_id=call_id,
                    input_ids=(source_id,),
                    params=tool_event_derivation_params(event),
                )
            )
            return call_id
        except Exception as exc:
            warn_trace_failure_once("record_tool_call", exc)
            return None

    def record_tool_result(
        self,
        trace: PromptTrace,
        call_event: dict[str, Any],
        result_event: dict[str, Any],
    ) -> ObjectId | None:
        """Record a first-class tool result derived from a tool call."""
        store = self.store()
        if store is None:
            return None
        call_object_id = str(call_event.get("tool_call_object_id") or "")
        if not call_object_id:
            call_object_id = self.record_tool_call(trace, call_event) or ""
        if not call_object_id:
            return None
        try:
            result_id = store.put_object(
                Object(
                    kind="tool_result",
                    schema="zeta.tool_result.v1",
                    data=tool_result_object_data(result_event),
                    links=(call_object_id,),
                )
            )
            store.record_derivation(
                Derivation(
                    producer="ToolExecution",
                    output_id=result_id,
                    input_ids=(call_object_id,),
                    params=tool_event_derivation_params(result_event),
                )
            )
            return result_id
        except Exception as exc:
            warn_trace_failure_once("record_tool_result", exc)
            return None

    def _skills_for(self, allowed_capabilities: Iterable[str] | None) -> list[Skill]:
        enabled = enabled_capability_ids(allowed_capabilities)
        cached = self._skills.get(enabled)
        if cached is None:
            cached = available_skills() if can_read_skill_files(enabled) else []
            self._skills[enabled] = cached
        return cached

    def store(self) -> Store | None:
        return self._store

    def _build_traced_prompt(
        self,
        components: list[PromptComponent],
        *,
        tools: list[dict[str, Any]],
        tool_choice: str | dict[str, Any],
        max_tokens: int,
        selected_model: str | None,
        thinking: str | None,
    ) -> PreparedPrompt:
        try:
            store = self.store()
            with store.batch() if store is not None else nullcontext():
                stored_components = self._store_components(components)
                transformed_components = self.transform.apply(stored_components)
                traced_components = self._store_transform_outputs(
                    transformed_components
                )
                return self._prepared_prompt(
                    traced_components,
                    tools=tools,
                    tool_choice=tool_choice,
                    max_tokens=max_tokens,
                    selected_model=selected_model,
                    thinking=thinking,
                )
        except Exception as exc:
            warn_trace_failure_once("build_prompt", exc)
            messages = component_messages(components)
            return PreparedPrompt(
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                payload=chat_completion_request_body(
                    messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    max_tokens=max_tokens,
                    selected_model=selected_model,
                    thinking=thinking,
                ),
            )

    def _prepared_prompt(
        self,
        components: list[PromptComponent],
        *,
        tools: list[dict[str, Any]],
        tool_choice: str | dict[str, Any],
        max_tokens: int,
        selected_model: str | None,
        thinking: str | None,
    ) -> PreparedPrompt:
        messages = component_messages(components)
        payload = chat_completion_request_body(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
            selected_model=selected_model,
            thinking=thinking,
        )
        component_ids = stored_component_ids(components)
        prompt_id = self._store_prompt_object(
            payload,
            component_ids,
            max_tokens=max_tokens,
            selected_model=selected_model,
            thinking=thinking,
        )
        return PreparedPrompt(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            payload=payload,
            prompt_object_id=prompt_id,
            component_object_ids=component_ids,
        )

    def _store_components(
        self,
        components: list[PromptComponent],
    ) -> list[PromptComponent]:
        store = self.store()
        if store is None:
            return list(components)
        stored = []
        for component in components:
            if component.object_id is None:
                object_id = store.put_object(prompt_component_object(component))
                stored.append(replace(component, object_id=object_id))
            else:
                stored.append(component)
        return stored

    def _store_transform_outputs(
        self,
        components: list[PromptComponent],
    ) -> list[PromptComponent]:
        store = self.store()
        if store is None:
            return list(components)
        stored = []
        producer = str(getattr(self.transform, "producer", "") or "")
        for component in components:
            is_new_output = component.object_id is None
            if is_new_output:
                object_id = store.put_object(prompt_component_object(component))
                component = replace(component, object_id=object_id)
            if producer and is_new_output and component.links:
                store.record_derivation(
                    Derivation(
                        producer=producer,
                        output_id=component.object_id or "",
                        input_ids=component.links,
                        params={},
                    )
                )
            stored.append(component)
        return stored

    def _store_prompt_object(
        self,
        payload: dict[str, Any],
        component_ids: tuple[ObjectId, ...],
        *,
        max_tokens: int,
        selected_model: str | None,
        thinking: str | None,
    ) -> ObjectId | None:
        store = self.store()
        if store is None:
            return None
        # The exact payload is reconstructible from the linked components;
        # embedding it here grew the store quadratically with turns.
        prompt_id = store.put_object(
            Object(
                kind="prompt",
                schema="zeta.prompt.v1",
                data={"payload_sha256": payload_sha256(payload)},
                links=component_ids,
            )
        )
        store.record_derivation(
            Derivation(
                producer="PromptBuilder",
                output_id=prompt_id,
                input_ids=component_ids,
                params={
                    "max_tokens": max_tokens,
                    "selected_model": selected_model,
                    "thinking": thinking,
                },
            )
        )
        store.set_ref("prompt/current", prompt_id)
        return prompt_id


def stored_component_ids(components: list[PromptComponent]) -> tuple[ObjectId, ...]:
    """Return the trace ids of the components that made it into the store."""
    return tuple(
        component.object_id
        for component in components
        if component.object_id is not None
    )


def payload_sha256(payload: dict[str, Any]) -> str:
    """Return the content address of a model request payload."""
    return content_hash(canonical_json(payload))


@dataclass(frozen=True)
class ReconstructedPrompt:
    """A model request rebuilt from a prompt object's component closure."""

    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    max_tokens: int
    selected_model: str | None
    thinking: str | None
    payload_verified: bool


def reconstructed_prompt_request(
    store: Store,
    prompt_id: ObjectId,
) -> ReconstructedPrompt | None:
    """Rebuild the exact request a prompt object hashed, and verify it.

    Messages come from the linked components in order, tool descriptors
    from the `tool_descriptor_set` component, and `max_tokens`/model from
    the prompt's builder derivation. `payload_verified` says whether the
    rebuilt payload hashes to the stored `payload_sha256`.
    """
    prompt = store.get_object(prompt_id)
    if prompt is None or prompt.kind != "prompt":
        return None
    messages: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    for component_id in prompt.links:
        component = store.get_object(component_id)
        if component is None:
            continue
        message = component.data.get("message")
        if isinstance(message, dict):
            messages.append(message)
        if component.kind == "tool_descriptor_set":
            raw_tools = component.data.get("tools")
            if isinstance(raw_tools, list):
                tools = raw_tools
    max_tokens, selected_model, thinking = prompt_builder_params(store, prompt_id)
    payload = chat_completion_request_body(
        messages,
        tools=tools,
        tool_choice="auto",
        max_tokens=max_tokens,
        selected_model=selected_model,
        thinking=thinking,
    )
    expected = str(prompt.data.get("payload_sha256") or "")
    return ReconstructedPrompt(
        messages=messages,
        tools=tools,
        max_tokens=max_tokens,
        selected_model=selected_model,
        thinking=thinking,
        payload_verified=bool(expected) and payload_sha256(payload) == expected,
    )


def prompt_builder_params(
    store: Store,
    prompt_id: ObjectId,
) -> tuple[int, str | None, str | None]:
    """Return the max_tokens, model, and thinking the builder recorded.

    A derivation without a `thinking` param predates the setting; those
    prompts were built with thinking disabled, so absence means `"none"`.
    """
    for derivation in store.derivations_for_output(prompt_id):
        if derivation.producer != "PromptBuilder":
            continue
        max_tokens = derivation.params.get("max_tokens")
        selected_model = derivation.params.get("selected_model")
        thinking = (
            derivation.params["thinking"] if "thinking" in derivation.params else "none"
        )
        return (
            max_tokens
            if isinstance(max_tokens, int) and not isinstance(max_tokens, bool)
            else DEFAULT_MAX_COMPLETION_TOKENS,
            selected_model if isinstance(selected_model, str) else None,
            thinking if isinstance(thinking, str) else None,
        )
    return DEFAULT_MAX_COMPLETION_TOKENS, None, None


def tool_call_object_data(event: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tool_call_id": str(event.get("tool_call_id") or event.get("id") or ""),
        "name": str(event.get("name") or ""),
        "input": event.get("input") if isinstance(event.get("input"), dict) else {},
    }
    arguments = event.get("arguments")
    if isinstance(arguments, str):
        data["arguments"] = arguments
    return data


def tool_result_object_data(event: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tool_call_id": str(event.get("tool_call_id") or ""),
        "name": str(event.get("name") or ""),
    }
    result = event.get("result")
    if isinstance(result, dict):
        data["result"] = result
    model_telemetry = event.get("model_telemetry")
    if isinstance(model_telemetry, dict):
        data["model_telemetry"] = model_telemetry
    return data


def tool_event_derivation_params(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool_call_id": str(event.get("tool_call_id") or event.get("id") or ""),
        "name": str(event.get("name") or ""),
    }
