"""Model profiles and protocol clients behind one package surface.

Profile discovery and selection load eagerly; the transport surface loads
on first attribute access so that status paths importing this package
stay free of the HTTP client and its jsonschema dependency.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from .chat_completions import (
        DEFAULT_MAX_COMPLETION_TOKENS,
        ChatCompletionStreamSink,
        chat_completion_request_body,
        endpoint_reachable,
        ensure_server,
        model_endpoint_open,
        model_endpoint_valid,
        request_model_metadata,
    )
    from .responses import set_responses_session_id_factory

_TRANSPORT_EXPORTS = frozenset(
    {
        "ChatCompletionStreamSink",
        "DEFAULT_MAX_COMPLETION_TOKENS",
        "chat_completion_request_body",
        "endpoint_reachable",
        "ensure_server",
        "model_endpoint_open",
        "model_endpoint_valid",
        "request_model_metadata",
    }
)

__all__ = [
    "CHAT_COMPLETIONS_API",
    "CODEX_RESPONSES_API",
    "ChatCompletionStreamSink",
    "DEFAULT_CODEX_BASE_URL",
    "DEFAULT_MAX_COMPLETION_TOKENS",
    "DEFAULT_MODEL_NAME",
    "DEFAULT_MODEL_URL",
    "MODEL_APIS",
    "THINKING_EFFORTS",
    "ModelCatalog",
    "ModelDiagnostic",
    "ModelProfile",
    "ModelResolution",
    "ModelSelection",
    "ModelSource",
    "active_model_profile",
    "active_model_selection",
    "chat_completion_messages",
    "chat_completion_request_body",
    "chat_structured_output",
    "clear_active_model_profile",
    "configured_default_selection",
    "default_model_selection",
    "endpoint_reachable",
    "ensure_server",
    "load_model_profiles",
    "model_endpoint_open",
    "model_endpoint_valid",
    "model_name",
    "model_selection_event",
    "model_url",
    "request_model_metadata",
    "resolve_active_model",
    "resolve_model_profile",
    "set_active_model_profile",
    "set_responses_session_id_factory",
    "user_models_config_path",
]


def set_responses_session_id_factory(factory: Any) -> None:
    from . import responses

    responses.set_responses_session_id_factory(factory)


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


def __getattr__(name: str) -> Any:
    if name in _TRANSPORT_EXPORTS:
        from . import chat_completions

        return getattr(chat_completions, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
