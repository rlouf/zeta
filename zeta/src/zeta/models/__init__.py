"""High-level model gateway helpers."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable
from typing import Any

import zeta.models.profiles as _profiles
import zeta.models.types as _model_types


class DefaultModelGateway:
    def available(self, config: Any) -> bool:
        if getattr(config, "model_api", None) == _profiles.CODEX_RESPONSES_API:
            return True
        from zeta.models.chat_completions import model_endpoint_open

        model_url = getattr(config, "model_url", None)
        if model_url is None:
            return model_endpoint_open()
        return model_endpoint_open(model_url)

    async def generate(
        self,
        model_input: _model_types.ModelInput,
        config: Any,
        *,
        stream: Any | None = None,
        telemetry_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> _model_types.ModelOutput:
        api = getattr(config, "model_api", None)
        options = {
            "api": api,
            "tools": model_input.tools or [],
            "tool_choice": model_input.tool_choice,
            "selected_model": getattr(config, "model_name", None),
            "selected_url": getattr(config, "model_url", None),
            "stream_sink": stream,
            "telemetry_sink": telemetry_sink,
            "thinking": getattr(config, "thinking", None),
        }
        if api == _profiles.CODEX_RESPONSES_API:
            options["session_id"] = getattr(config, "model_session_id", None)
        assistant = await asyncio.to_thread(
            chat_completion_messages,
            model_input.messages,
            **options,
        )
        return _model_types.ModelOutput(message=assistant)


__all__ = [
    "DefaultModelGateway",
    "chat_completion_messages",
    "chat_structured_output",
]


def chat_completion_messages(
    messages: list[dict[str, Any]],
    *,
    api: str | None = None,
    **options: Any,
) -> dict[str, Any]:
    """Request one assistant message from the selected protocol client."""
    if api is None or api == _profiles.CHAT_COMPLETIONS_API:
        chat_completions = importlib.import_module("zeta.models.chat_completions")
        return chat_completions.chat_completion_messages(messages, **options)
    if api == _profiles.CODEX_RESPONSES_API:
        responses = importlib.import_module("zeta.models.responses")
        return responses.codex_completion_messages(messages, **options)
    raise ValueError(f"unknown model api: {api!r}")


def chat_structured_output(
    messages: list[dict[str, Any]],
    *,
    api: str | None = None,
    **options: Any,
) -> dict[str, Any]:
    """Request one schema-validated JSON object from the selected client."""
    if api is None or api == _profiles.CHAT_COMPLETIONS_API:
        options.pop("session_id", None)
        chat_completions = importlib.import_module("zeta.models.chat_completions")
        return chat_completions.chat_structured_output(messages, **options)
    if api == _profiles.CODEX_RESPONSES_API:
        responses = importlib.import_module("zeta.models.responses")
        return responses.codex_structured_output(messages, **options)
    raise ValueError(f"unknown model api: {api!r}")
