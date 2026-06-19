"""Prompt builder and trace recording for Zeta.

Prompt component order is a public contract for prefix-cache friendliness:
system_prompt, tool descriptors, project context, then volatile components.
"""

import json
from collections.abc import Iterable
from contextlib import nullcontext
from dataclasses import dataclass, replace
from typing import Any

from zeta.capabilities.base import content_hash
from zeta.context.components import (
    PromptComponent,
    component_messages,
    prompt_component_object,
    prompt_components,
)
from zeta.context.system import can_read_skill_files, enabled_capability_ids
from zeta.context.transforms import NoOpPromptTransform, PromptTransform
from zeta.events import DraftEvent, Event, draft_event_id
from zeta.models import ModelInput
from zeta.models.chat_completions import (
    DEFAULT_MAX_COMPLETION_TOKENS,
    chat_completion_request_body,
)
from zeta.skills import Skill, available_skills
from zeta.store.substrate import (
    Store,
    warn_trace_failure_once,
)
from zeta.substrate import Derivation, Object, ObjectId


@dataclass(frozen=True)
class PreparedPrompt:
    """A model-ready prompt plus trace ids for its stored object graph."""

    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    tool_choice: str | dict[str, Any]
    payload: dict[str, Any]
    prompt_object_id: ObjectId | None = None
    component_object_ids: tuple[ObjectId, ...] = ()


@dataclass(frozen=True)
class PromptPlan:
    """Pure prompt components plus render options."""

    components: tuple[PromptComponent, ...]
    tools: tuple[dict[str, Any], ...]
    tool_choice: str | dict[str, Any]
    max_tokens: int
    selected_model: str | None = None
    thinking: str | None = None


@dataclass(frozen=True)
class StoredPrompt:
    """A committed prompt plan with trace object ids."""

    plan: PromptPlan
    components: tuple[PromptComponent, ...]
    prompt_object_id: ObjectId | None = None
    component_object_ids: tuple[ObjectId, ...] = ()


@dataclass(frozen=True)
class TraceProjection:
    """Trace ids derived from replaying domain events."""

    prompt_object_ids: dict[str, ObjectId]
    assistant_message_ids: dict[str, ObjectId]
    tool_call_object_ids: dict[str, ObjectId]
    tool_result_object_ids: dict[str, ObjectId]


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

    def plan_prompt(
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
    ) -> PromptPlan:
        return plan_prompt(
            objective,
            timeline,
            system=system,
            allowed_capabilities=allowed_capabilities,
            context=context,
            current_events=current_events,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
            selected_model=selected_model,
            thinking=thinking,
            skills=self._skills_for(allowed_capabilities),
        )

    def commit_prompt_plan(self, plan: PromptPlan) -> StoredPrompt:
        return commit_prompt_plan(
            plan,
            self.store(),
            transform=self.transform,
        )

    def _skills_for(self, allowed_capabilities: Iterable[str] | None) -> list[Skill]:
        enabled = enabled_capability_ids(allowed_capabilities)
        cached = self._skills.get(enabled)
        if cached is None:
            cached = available_skills() if can_read_skill_files(enabled) else []
            self._skills[enabled] = cached
        return cached

    def store(self) -> Store | None:
        return self._store


def project_trace_events(
    events: Iterable[Event], store: Store | None
) -> TraceProjection:
    projection = TraceProjection({}, {}, {}, {})
    if store is None:
        return projection
    latest_assistant_id: ObjectId | None = None
    for event in events:
        timeline_type = event_timeline_type(event)
        if timeline_type == "model":
            latest_assistant_id = project_model_event(event, store, projection)
            continue
        if timeline_type == "tool_call":
            project_tool_call_event(
                event,
                store,
                projection,
                latest_assistant_id=latest_assistant_id,
            )
            continue
        if timeline_type == "tool_result":
            project_tool_result_event(event, store, projection)
    return projection


def project_trace_drafts(
    drafts: Iterable[DraftEvent],
    store: Store | None,
) -> TraceProjection:
    return project_trace_events(
        (
            Event(
                id=draft_event_id(draft) or "",
                event_type=draft.event_type,
                source=draft.source,
                payload=draft.payload,
                idempotency_key=draft.idempotency_key,
                caused_by=draft.caused_by,
                session_id=draft.session_id,
                turn_id=draft.turn_id,
                timestamp_micros=0,
            )
            for draft in drafts
        ),
        store,
    )


def event_timeline_type(event: Event) -> str:
    view_type = event.payload.get("_timeline_type")
    if isinstance(view_type, str) and view_type:
        return view_type
    prefix = "zeta."
    if event.event_type.startswith(prefix):
        return event.event_type[len(prefix) :]
    return event.event_type


def project_model_event(
    event: Event,
    store: Store,
    projection: TraceProjection,
) -> ObjectId | None:
    prompt_id = optional_object_id(event.payload.get("prompt_object_id"))
    if prompt_id is None:
        return None
    assistant_id = store.put_object(
        Object(
            kind="assistant_message",
            schema="zeta.model_output.v1",
            data=model_trace_data(event),
            links=(prompt_id,),
        )
    )
    store.record_derivation(
        Derivation(
            producer="ModelResponse",
            output_id=assistant_id,
            input_ids=(prompt_id,),
            params={},
        )
    )
    projection.prompt_object_ids[event.id] = prompt_id
    projection.assistant_message_ids[event.id] = assistant_id
    return assistant_id


def project_tool_call_event(
    event: Event,
    store: Store,
    projection: TraceProjection,
    *,
    latest_assistant_id: ObjectId | None,
) -> ObjectId | None:
    source_id = (
        projection.assistant_message_ids.get(event.caused_by or "")
        or latest_assistant_id
    )
    if source_id is None:
        return None
    payload = dict(event.payload)
    call_id = store.put_object(
        Object(
            kind="tool_call",
            schema="zeta.tool_call.v1",
            data=tool_call_object_data(payload),
            links=(source_id,),
        )
    )
    store.record_derivation(
        Derivation(
            producer="ToolCallProjection",
            output_id=call_id,
            input_ids=(source_id,),
            params=tool_event_derivation_params(payload),
        )
    )
    projection.tool_call_object_ids[event.id] = call_id
    tool_call_id = payload.get("tool_call_id")
    if isinstance(tool_call_id, str) and tool_call_id:
        projection.tool_call_object_ids[tool_call_id] = call_id
    return call_id


def project_tool_result_event(
    event: Event,
    store: Store,
    projection: TraceProjection,
) -> ObjectId | None:
    payload = dict(event.payload)
    tool_call_id = payload.get("tool_call_id")
    call_object_id = (
        projection.tool_call_object_ids.get(tool_call_id)
        if isinstance(tool_call_id, str)
        else None
    )
    if call_object_id is None:
        return None
    result_id = store.put_object(
        Object(
            kind="tool_result",
            schema="zeta.tool_result.v1",
            data=tool_result_object_data(payload),
            links=(call_object_id,),
        )
    )
    store.record_derivation(
        Derivation(
            producer="ToolExecution",
            output_id=result_id,
            input_ids=(call_object_id,),
            params=tool_event_derivation_params(payload),
        )
    )
    projection.tool_result_object_ids[event.id] = result_id
    return result_id


def optional_object_id(value: Any) -> ObjectId | None:
    return value if isinstance(value, str) and value.startswith("sha256:") else None


def model_trace_data(event: Event) -> dict[str, Any]:
    message: dict[str, Any] = {}
    content = event.payload.get("content")
    if isinstance(content, str):
        message["content"] = content
    reasoning = event.payload.get("reasoning")
    if isinstance(reasoning, str):
        message["reasoning_content"] = reasoning
    tool_calls = event.payload.get("tool_calls")
    if isinstance(tool_calls, list):
        message["tool_calls"] = [call for call in tool_calls if isinstance(call, dict)]
    return {"message": dict(message), "model_output": {"message": dict(message)}}


def plan_prompt(
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
    skills: list[Skill] | None = None,
) -> PromptPlan:
    components = prompt_components(
        objective,
        timeline,
        system=system,
        allowed_capabilities=allowed_capabilities,
        context=context,
        current_events=current_events,
        tools=tools,
        skills=skills,
    )
    return PromptPlan(
        components=tuple(components),
        tools=tuple(tools or []),
        tool_choice=tool_choice,
        max_tokens=max_tokens,
        selected_model=selected_model,
        thinking=thinking,
    )


def commit_prompt_plan(
    plan: PromptPlan,
    store: Store | None,
    *,
    transform: PromptTransform | None = None,
) -> StoredPrompt:
    prompt_transform = transform or NoOpPromptTransform()
    try:
        with store.batch() if store is not None else nullcontext():
            stored_components = store_components(list(plan.components), store)
            transformed_components = prompt_transform.apply(stored_components)
            traced_components = store_transform_outputs(
                transformed_components,
                store,
                transform=prompt_transform,
            )
            component_ids = stored_component_ids(traced_components)
            prompt_id = store_prompt_object(
                plan,
                traced_components,
                store,
                component_ids=component_ids,
            )
            return StoredPrompt(
                plan=plan,
                components=tuple(traced_components),
                prompt_object_id=prompt_id,
                component_object_ids=component_ids,
            )
    except Exception as exc:
        warn_trace_failure_once("build_prompt", exc)
        return StoredPrompt(plan=plan, components=plan.components)


def render_model_input(prompt: PromptPlan | StoredPrompt) -> ModelInput:
    plan = prompt.plan if isinstance(prompt, StoredPrompt) else prompt
    components = (
        prompt.components if isinstance(prompt, StoredPrompt) else plan.components
    )
    return ModelInput(
        messages=component_messages(list(components)),
        tools=list(plan.tools),
        tool_choice=plan.tool_choice,
        max_tokens=plan.max_tokens,
        selected_model=plan.selected_model,
        thinking=plan.thinking,
    )


def prepared_prompt_from(
    prompt: PromptPlan | StoredPrompt,
    *,
    model_input: ModelInput | None = None,
) -> PreparedPrompt:
    if model_input is None:
        model_input = render_model_input(prompt)
    prompt_id = prompt.prompt_object_id if isinstance(prompt, StoredPrompt) else None
    component_ids = (
        prompt.component_object_ids if isinstance(prompt, StoredPrompt) else ()
    )
    return PreparedPrompt(
        messages=model_input.messages,
        tools=model_input.tools or [],
        tool_choice=model_input.tool_choice,
        payload=chat_completion_request_body(
            model_input.messages,
            tools=model_input.tools or [],
            tool_choice=model_input.tool_choice,
            max_tokens=model_input.max_tokens or DEFAULT_MAX_COMPLETION_TOKENS,
            selected_model=model_input.selected_model,
            thinking=model_input.thinking,
        ),
        prompt_object_id=prompt_id,
        component_object_ids=component_ids,
    )


def store_components(
    components: list[PromptComponent],
    store: Store | None,
) -> list[PromptComponent]:
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


def store_transform_outputs(
    components: list[PromptComponent],
    store: Store | None,
    *,
    transform: PromptTransform,
) -> list[PromptComponent]:
    if store is None:
        return list(components)
    stored = []
    producer = str(getattr(transform, "producer", "") or "")
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


def store_prompt_object(
    plan: PromptPlan,
    components: list[PromptComponent],
    store: Store | None,
    *,
    component_ids: tuple[ObjectId, ...],
) -> ObjectId | None:
    if store is None:
        return None
    payload = chat_completion_request_body(
        component_messages(components),
        tools=list(plan.tools),
        tool_choice=plan.tool_choice,
        max_tokens=plan.max_tokens,
        selected_model=plan.selected_model,
        thinking=plan.thinking,
    )
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
                "max_tokens": plan.max_tokens,
                "selected_model": plan.selected_model,
                "thinking": plan.thinking,
            },
        )
    )
    current = store.get_ref("prompt/current")
    expected = current.object_id if current is not None else None
    store.move_ref("prompt/current", expected, prompt_id)
    return prompt_id


def stored_component_ids(components: Iterable[PromptComponent]) -> tuple[ObjectId, ...]:
    """Return the trace ids of the components that made it into the store."""
    return tuple(
        component.object_id
        for component in components
        if component.object_id is not None
    )


def payload_sha256(payload: dict[str, Any]) -> str:
    """Return the content address of a model request payload."""
    return content_hash(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


@dataclass(frozen=True)
class ReconstructedPrompt:
    """A model request rebuilt from a prompt object's component closure."""

    plan: PromptPlan
    model_input: ModelInput
    payload_verified: bool

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self.model_input.messages

    @property
    def tools(self) -> list[dict[str, Any]]:
        return self.model_input.tools or []

    @property
    def max_tokens(self) -> int:
        return self.model_input.max_tokens or DEFAULT_MAX_COMPLETION_TOKENS

    @property
    def selected_model(self) -> str | None:
        return self.model_input.selected_model

    @property
    def thinking(self) -> str | None:
        return self.model_input.thinking


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
    components: list[PromptComponent] = []
    tools: list[dict[str, Any]] = []
    for component_id in prompt.links:
        component = store.get_object(component_id)
        if component is None:
            continue
        message = component.data.get("message")
        if isinstance(message, dict):
            components.append(
                PromptComponent(
                    kind=component.kind,
                    data=dict(component.data),
                    message=message,
                    source_object_id=component_id,
                )
            )
        if component.kind == "tool_descriptor_set":
            raw_tools = component.data.get("tools")
            if isinstance(raw_tools, list):
                tools = raw_tools
    max_tokens, selected_model, thinking = prompt_builder_params(store, prompt_id)
    plan = PromptPlan(
        components=tuple(components),
        tools=tuple(tools),
        tool_choice="auto",
        max_tokens=max_tokens,
        selected_model=selected_model,
        thinking=thinking,
    )
    model_input = render_model_input(plan)
    payload = chat_completion_request_body(
        model_input.messages,
        tools=model_input.tools or [],
        tool_choice=model_input.tool_choice,
        max_tokens=model_input.max_tokens or DEFAULT_MAX_COMPLETION_TOKENS,
        selected_model=model_input.selected_model,
        thinking=model_input.thinking,
    )
    expected = str(prompt.data.get("payload_sha256") or "")
    return ReconstructedPrompt(
        plan=plan,
        model_input=model_input,
        payload_verified=bool(expected) and payload_sha256(payload) == expected,
    )


def prompt_builder_params(
    store: Store,
    prompt_id: ObjectId,
) -> tuple[int, str | None, str | None]:
    """Return the max_tokens, model, and thinking the builder recorded."""
    for derivation in store.derivations_for_output(prompt_id):
        if derivation.producer != "PromptBuilder":
            continue
        max_tokens = derivation.params.get("max_tokens")
        selected_model = derivation.params.get("selected_model")
        thinking = derivation.params.get("thinking")
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
