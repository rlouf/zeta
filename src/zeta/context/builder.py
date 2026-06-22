"""Prompt builder and trace recording for Zeta.

Prompt component order is a public contract for prefix-cache friendliness:
system_prompt, tool descriptors, project context, then volatile components.
"""

import json
from collections.abc import Iterable
from contextlib import nullcontext
from dataclasses import dataclass, replace
from typing import Any

from zeta.capabilities.execution import content_hash
from zeta.context.components import (
    PromptComponent,
    component_messages,
    prompt_component_object,
    prompt_components,
)
from zeta.context.transforms import NoOpPromptTransform, PromptTransform
from zeta.models.chat_completions import (
    DEFAULT_MAX_COMPLETION_TOKENS,
    chat_completion_request_body,
)
from zeta.models.types import ModelInput
from zeta.records.objects import Derivation, Object, ObjectId
from zeta.records.stores import (
    Store,
    warn_trace_failure_once,
)


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
        )

    def commit_prompt_plan(self, plan: PromptPlan) -> StoredPrompt:
        return commit_prompt_plan(
            plan,
            self.store(),
            transform=self.transform,
        )

    def store(self) -> Store | None:
        return self._store


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
) -> PromptPlan:
    components = prompt_components(
        objective,
        timeline,
        system=system,
        allowed_capabilities=allowed_capabilities,
        context=context,
        current_events=current_events,
        tools=tools,
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
