"""Model profiles and protocol clients behind one package surface.

Profile discovery and selection load eagerly. Transport-specific helpers live
in their transport modules.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from zeta.kernel.models import ModelInput as _ModelInput
from zeta.kernel.models import ModelOutput as _ModelOutput
from zeta.models.profiles import (
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


class DefaultModelGateway:
    def available(self, config: Any) -> bool:
        if getattr(config, "model_api", None) == CODEX_RESPONSES_API:
            return True
        from zeta.models.chat_completions import model_endpoint_open

        model_url = getattr(config, "model_url", None)
        if model_url is None:
            return model_endpoint_open()
        return model_endpoint_open(model_url)

    async def generate(
        self,
        model_input: _ModelInput,
        config: Any,
        *,
        stream: Any | None = None,
        telemetry_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> _ModelOutput:
        assistant = await asyncio.to_thread(
            chat_completion_messages,
            model_input.messages,
            api=getattr(config, "model_api", None),
            tools=model_input.tools or [],
            tool_choice=model_input.tool_choice,
            selected_model=getattr(config, "model_name", None),
            selected_url=getattr(config, "model_url", None),
            session_id=getattr(config, "model_session_id", None),
            stream_sink=stream,
            telemetry_sink=telemetry_sink,
            thinking=getattr(config, "thinking", None),
        )
        return _ModelOutput(message=assistant)


__all__ = [
    "CHAT_COMPLETIONS_API",
    "CODEX_RESPONSES_API",
    "DEFAULT_CODEX_BASE_URL",
    "DEFAULT_MODEL_NAME",
    "DEFAULT_MODEL_URL",
    "DefaultModelGateway",
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
        from zeta.models import chat_completions

        return chat_completions.chat_completion_messages(messages, **options)
    if api == CODEX_RESPONSES_API:
        from zeta.models import responses

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
        from zeta.models import chat_completions

        return chat_completions.chat_structured_output(messages, **options)
    if api == CODEX_RESPONSES_API:
        from zeta.models import responses

        return responses.codex_structured_output(messages, **options)
    raise ValueError(f"unknown model api: {api!r}")
