"""Prompt builder and trace recording for Zeta."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable

from ..model import chat_completion_request_body
from ..trace import Derivation, Object, ObjectId, PromptTrace, Store, default_store
from .components import (
    PromptComponent,
    component_messages,
    prompt_component_object,
    prompt_components,
    update_component_refs,
)
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
        self._store_initialized = store is not None
        self.transform = transform or NoOpPromptTransform()

    def build(
        self,
        objective: str,
        transcript: list[dict[str, Any]],
        *,
        system: str | None = None,
        allowed_tools: Iterable[str] | None = None,
        context: str = "",
        current_events: Iterable[dict[str, Any]] = (),
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] = "auto",
        max_tokens: int = 1200,
        selected_model: str | None = None,
    ) -> PreparedPrompt:
        components = prompt_components(
            objective,
            transcript,
            system=system,
            allowed_tools=allowed_tools,
            context=context,
            current_events=current_events,
            tools=tools,
        )
        fallback_tools = tools or []
        return self._build_traced_prompt(
            components,
            tools=fallback_tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
            selected_model=selected_model,
        )

    def record_assistant_message(
        self,
        prepared: PreparedPrompt,
        assistant: dict[str, Any],
    ) -> PromptTrace | None:
        store = self.store()
        if store is None or prepared.prompt_object_id is None:
            return None
        try:
            assistant_id = store.put_object(
                Object(
                    kind="assistant_message",
                    schema="zeta.assistant_output.v1",
                    data={"message": assistant},
                    links=(prepared.prompt_object_id,),
                )
            )
            store.record_derivation(
                Derivation(
                    producer="SigilModelResponse:v1",
                    output_id=assistant_id,
                    input_ids=(prepared.prompt_object_id,),
                    params={},
                )
            )
            return PromptTrace(
                prompt_object_id=prepared.prompt_object_id,
                assistant_message_object_id=assistant_id,
                component_object_ids=prepared.component_object_ids,
            )
        except Exception:
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
                    producer="SigilToolCallProjection:v1",
                    output_id=call_id,
                    input_ids=(source_id,),
                    params=tool_event_derivation_params(event),
                )
            )
            return call_id
        except Exception:
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
                    producer="SigilToolExecution:v1",
                    output_id=result_id,
                    input_ids=(call_object_id,),
                    params=tool_event_derivation_params(result_event),
                )
            )
            return result_id
        except Exception:
            return None

    def store(self) -> Store | None:
        if self._store_initialized:
            return self._store
        self._store_initialized = True
        try:
            self._store = default_store()
        except Exception:
            self._store = None
        return self._store

    def _build_traced_prompt(
        self,
        components: list[PromptComponent],
        *,
        tools: list[dict[str, Any]],
        tool_choice: str | dict[str, Any],
        max_tokens: int,
        selected_model: str | None,
    ) -> PreparedPrompt:
        try:
            stored_components = self._store_components(components)
            transformed_components = self.transform.apply(stored_components)
            traced_components = self._store_transform_outputs(transformed_components)
            return self._prepared_prompt(
                traced_components,
                tools=tools,
                tool_choice=tool_choice,
                max_tokens=max_tokens,
                selected_model=selected_model,
            )
        except Exception:
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
    ) -> PreparedPrompt:
        messages = component_messages(components)
        payload = chat_completion_request_body(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
            selected_model=selected_model,
        )
        prompt_id = self._store_prompt_object(
            payload,
            components,
            max_tokens=max_tokens,
            selected_model=selected_model,
        )
        component_ids = tuple(
            component.object_id
            for component in components
            if component.object_id is not None
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
        components: list[PromptComponent],
        *,
        max_tokens: int,
        selected_model: str | None,
    ) -> ObjectId | None:
        store = self.store()
        if store is None:
            return None
        component_ids = tuple(
            component.object_id
            for component in components
            if component.object_id is not None
        )
        prompt_id = store.put_object(
            Object(
                kind="prompt",
                schema="zeta.prompt.v1",
                data={"payload": payload},
                links=component_ids,
            )
        )
        resolved_refs = update_component_refs(store, components)
        store.record_derivation(
            Derivation(
                producer="SigilPromptBuilder:v1",
                output_id=prompt_id,
                input_ids=component_ids,
                resolved_refs=resolved_refs,
                params={
                    "max_tokens": max_tokens,
                    "selected_model": selected_model,
                },
            )
        )
        store.set_ref("prompt/current", prompt_id)
        return prompt_id


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
