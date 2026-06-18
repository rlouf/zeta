"""Model profiles and protocol clients behind one package surface.

Profile discovery and selection load eagerly. Transport-specific helpers live
in their transport modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .profiles import (
    CHAT_COMPLETIONS_API,
    CODEX_RESPONSES_API,
    DEFAULT_CODEX_BASE_URL,
    DEFAULT_MODEL_NAME,
    DEFAULT_MODEL_URL,
    MODEL_APIS,
    THINKING_EFFORTS,
    ModelCatalog,
    ModelDiagnostic,
    ModelProfile,
    ModelResolution,
    ModelSelection,
    ModelSource,
    active_model_profile,
    active_model_selection,
    clear_active_model_profile,
    configured_default_selection,
    default_model_selection,
    load_model_profiles,
    model_name,
    model_selection_event,
    model_url,
    resolve_active_model,
    resolve_model_profile,
    set_active_model_profile,
    user_models_config_path,
)


@dataclass(frozen=True)
class ModelInput:
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] = "auto"
    max_tokens: int | None = None
    selected_model: str | None = None
    selected_url: str | None = None
    session_id: str | None = None
    thinking: str | None = None


@dataclass(frozen=True)
class ModelUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> ModelUsage | None:
        usage = cls(
            prompt_tokens=token_count(value.get("prompt_tokens")),
            completion_tokens=token_count(value.get("completion_tokens")),
            total_tokens=token_count(value.get("total_tokens")),
        )
        if (
            usage.prompt_tokens is None
            and usage.completion_tokens is None
            and usage.total_tokens is None
        ):
            return None
        return usage

    def to_mapping(self) -> dict[str, int]:
        payload: dict[str, int] = {}
        if self.prompt_tokens is not None:
            payload["prompt_tokens"] = self.prompt_tokens
        if self.completion_tokens is not None:
            payload["completion_tokens"] = self.completion_tokens
        if self.total_tokens is not None:
            payload["total_tokens"] = self.total_tokens
        return payload


@dataclass(frozen=True)
class ModelOutput:
    message: dict[str, Any]
    finish_reason: str | None = None
    usage: ModelUsage | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    provider_replay_items: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_chat_completion(cls, payload: dict[str, Any]) -> ModelOutput:
        choices = payload.get("choices")
        first_choice = choices[0] if isinstance(choices, list) and choices else {}
        first_choice = first_choice if isinstance(first_choice, dict) else {}
        message = first_choice.get("message")
        message = dict(message) if isinstance(message, dict) else {}
        usage = payload.get("usage")
        return cls(
            message=message,
            finish_reason=optional_str(first_choice.get("finish_reason")),
            usage=ModelUsage.from_mapping(usage) if isinstance(usage, dict) else None,
            provider_metadata=provider_metadata(payload),
            provider_replay_items=provider_replay_items(message),
        )

    def to_trace_data(self) -> dict[str, Any]:
        model_output: dict[str, Any] = {"message": dict(self.message)}
        if self.finish_reason is not None:
            model_output["finish_reason"] = self.finish_reason
        if self.usage is not None:
            model_output["usage"] = self.usage.to_mapping()
        if self.provider_metadata:
            model_output["provider_metadata"] = dict(self.provider_metadata)
        if self.provider_replay_items:
            model_output["provider_replay_items"] = [
                dict(item) for item in self.provider_replay_items
            ]
        return {
            "message": dict(self.message),
            "model_output": model_output,
        }


def provider_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key in ("id", "object", "created", "model", "system_fingerprint")
        if (value := payload.get(key)) is not None
    }


def provider_replay_items(message: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    items = message.get("_responses_items")
    if not isinstance(items, list):
        return ()
    return tuple(item for item in items if isinstance(item, dict))


def optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def token_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


__all__ = [
    "CHAT_COMPLETIONS_API",
    "CODEX_RESPONSES_API",
    "DEFAULT_CODEX_BASE_URL",
    "DEFAULT_MODEL_NAME",
    "DEFAULT_MODEL_URL",
    "MODEL_APIS",
    "THINKING_EFFORTS",
    "ModelCatalog",
    "ModelDiagnostic",
    "ModelInput",
    "ModelOutput",
    "ModelProfile",
    "ModelResolution",
    "ModelSelection",
    "ModelSource",
    "ModelUsage",
    "active_model_profile",
    "active_model_selection",
    "chat_completion_messages",
    "chat_structured_output",
    "clear_active_model_profile",
    "configured_default_selection",
    "default_model_selection",
    "load_model_profiles",
    "model_name",
    "model_selection_event",
    "model_url",
    "resolve_active_model",
    "resolve_model_profile",
    "set_active_model_profile",
    "user_models_config_path",
]


def chat_completion_messages(
    messages: list[dict[str, Any]],
    *,
    api: str | None = None,
    **options: Any,
) -> dict[str, Any]:
    """Request one assistant message from the selected protocol client."""
    if api is None or api == CHAT_COMPLETIONS_API:
        from . import chat_completions

        return chat_completions.chat_completion_messages(messages, **options)
    if api == CODEX_RESPONSES_API:
        from . import responses

        return responses.codex_completion_messages(messages, **options)
    raise ValueError(f"unknown model api: {api!r}")


def chat_structured_output(
    messages: list[dict[str, Any]],
    *,
    api: str | None = None,
    **options: Any,
) -> dict[str, Any]:
    """Request one schema-validated JSON object from the selected client."""
    if api is None or api == CHAT_COMPLETIONS_API:
        from . import chat_completions

        return chat_completions.chat_structured_output(messages, **options)
    if api == CODEX_RESPONSES_API:
        from . import responses

        return responses.codex_structured_output(messages, **options)
    raise ValueError(f"unknown model api: {api!r}")
