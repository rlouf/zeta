"""High-level model gateway helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import zeta.models.chat_completions as _chat_completions
import zeta.models.endpoint as _endpoint
import zeta.models.profiles as _profiles
import zeta.models.responses as _responses
from zeta.models.types import ModelInput, ModelOutput, ModelRequest


class DefaultModelGateway:
    def available(self, request: ModelRequest) -> bool:
        """Return whether this protocol's endpoint can be reached.

        Only a local endpoint is probed. A hosted protocol reports its own
        failures, so probing it here would be a guess.
        """
        if not _profiles.probes_endpoint(request.api):
            return True
        if request.url is None:
            return _endpoint.model_endpoint_open()
        return _endpoint.model_endpoint_open(request.url)

    async def generate(
        self,
        model_input: ModelInput,
        request: ModelRequest,
        *,
        stream: Any | None = None,
        telemetry_sink: Callable[[dict[str, Any]], None] | None = None,
        should_stop: Callable[[], str | None] | None = None,
    ) -> ModelOutput:
        assistant = await chat_completion_messages(
            model_input.messages,
            request,
            tools=model_input.tools or [],
            tool_choice=model_input.tool_choice,
            stream_sink=stream,
            telemetry_sink=telemetry_sink,
            should_stop=should_stop,
        )
        return ModelOutput(message=assistant)


__all__ = [
    "DefaultModelGateway",
    "chat_completion_messages",
    "chat_structured_output",
]


async def chat_completion_messages(
    messages: list[dict[str, Any]],
    request: ModelRequest,
    **options: Any,
) -> dict[str, Any]:
    """Request one assistant message from the selected protocol client."""
    if request.api is None or request.api == _profiles.CHAT_COMPLETIONS_API:
        return await _chat_completions.chat_completion_messages(
            messages, request, **options
        )
    if request.api == _profiles.CODEX_RESPONSES_API:
        return await _responses.codex_completion_messages(messages, request, **options)
    raise ValueError(f"unknown model api: {request.api!r}")


async def chat_structured_output(
    messages: list[dict[str, Any]],
    request: ModelRequest,
    **options: Any,
) -> dict[str, Any]:
    """Request one schema-validated JSON object from the selected client."""
    if request.api is None or request.api == _profiles.CHAT_COMPLETIONS_API:
        return await _chat_completions.chat_structured_output(
            messages, request, **options
        )
    if request.api == _profiles.CODEX_RESPONSES_API:
        return await _responses.codex_structured_output(messages, request, **options)
    raise ValueError(f"unknown model api: {request.api!r}")
