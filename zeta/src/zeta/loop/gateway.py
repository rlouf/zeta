"""The model boundary for one run.

A run asks a gateway for one assistant message. These protocols and helpers
name that request, so the loop does not depend on a transport.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, Protocol

from zeta.events import DraftEvent
from zeta.loop.config import AgentConfig
from zeta.loop.streaming import ModelTurnStreamSink, StatusAwareModelStream
from zeta.loop.types import AgentEventSink
from zeta.models import DefaultModelGateway
from zeta.models.types import ModelInput, ModelOutput


class ModelStream(Protocol):
    def content_delta(self, text: str) -> None: ...

    def reasoning_delta(self, text: str) -> None: ...


class ModelGateway(Protocol):
    def available(self, config: AgentConfig) -> bool: ...

    async def generate(
        self,
        model_input: ModelInput,
        config: AgentConfig,
        *,
        stream: ModelStream | None = None,
        telemetry_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> ModelOutput: ...


def agent_model_endpoint_open(config: AgentConfig) -> bool:
    return DefaultModelGateway().available(config)


def run_model_metadata(config: AgentConfig) -> dict[str, str]:
    metadata = {
        "profile": config.model_profile,
        "model": config.model_name,
        "url": config.model_url,
        "api": config.model_api,
    }
    return {key: value for key, value in metadata.items() if value}


async def request_assistant_message(
    model_input: ModelInput,
    *,
    config: AgentConfig,
    model_gateway: ModelGateway | None = None,
    events: list[DraftEvent] | None = None,
    event_sink: AgentEventSink | None = None,
) -> tuple[ModelOutput, bool, dict[str, Any]]:
    model_telemetry: dict[str, Any] = {}
    recorded_events = events if events is not None else []
    turn_stream_sink = ModelTurnStreamSink(recorded_events, event_sink)
    gateway = model_gateway or DefaultModelGateway()
    status_factory = config.model_status_factory
    if status_factory is None:
        generated = gateway.generate(
            model_input,
            config,
            stream=turn_stream_sink,
            telemetry_sink=model_telemetry.update,
        )
        model_output = await generated if inspect.isawaitable(generated) else generated
    else:
        with status_factory() as status:
            generated = gateway.generate(
                model_input,
                config,
                stream=StatusAwareModelStream(turn_stream_sink, status),
                telemetry_sink=model_telemetry.update,
            )
            model_output = (
                await generated if inspect.isawaitable(generated) else generated
            )
    return (
        model_output,
        turn_stream_sink.streamed_content,
        model_telemetry,
    )
