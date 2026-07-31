"""The model boundary for one run.

A run asks a gateway for one assistant message. These protocols and helpers
name that request, so the loop does not depend on a transport.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from zeta.events import DraftEvent
from zeta.loop.config import AgentConfig
from zeta.loop.streaming import ModelTurnStreamSink, StatusAwareModelStream
from zeta.loop.types import AgentEventSink
from zeta.models import DefaultModelGateway
from zeta.models.types import ModelInput, ModelOutput, ModelRequest


class ModelStream(Protocol):
    def content_delta(self, text: str) -> None: ...

    def reasoning_delta(self, text: str) -> None: ...


class ModelGateway(Protocol):
    def available(self, request: ModelRequest) -> bool: ...

    async def generate(
        self,
        model_input: ModelInput,
        request: ModelRequest,
        *,
        stream: ModelStream | None = None,
        telemetry_sink: Callable[[dict[str, Any]], None] | None = None,
        should_stop: Callable[[], str | None] | None = None,
    ) -> ModelOutput: ...


def model_request_from(config: AgentConfig) -> ModelRequest:
    """Convert a run's config into what a backend needs.

    The models layer never reads a runtime object, so the conversion happens
    here, once.
    """
    return ModelRequest(
        api=config.model_api,
        model=config.model_name,
        url=config.model_url,
        thinking=config.thinking,
        session_id=config.model_session_id,
    )


def agent_model_endpoint_open(config: AgentConfig) -> bool:
    return DefaultModelGateway().available(model_request_from(config))


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
    should_stop: Callable[[], str | None] | None = None,
) -> tuple[ModelOutput, bool, dict[str, Any]]:
    model_telemetry: dict[str, Any] = {}
    recorded_events = events if events is not None else []
    turn_stream_sink = ModelTurnStreamSink(recorded_events, event_sink)
    gateway = model_gateway or DefaultModelGateway()
    status_factory = config.model_status_factory
    if status_factory is None:
        model_output = await gateway.generate(
            model_input,
            model_request_from(config),
            stream=turn_stream_sink,
            telemetry_sink=model_telemetry.update,
            should_stop=should_stop,
        )
    else:
        with status_factory() as status:
            model_output = await gateway.generate(
                model_input,
                model_request_from(config),
                stream=StatusAwareModelStream(turn_stream_sink, status),
                telemetry_sink=model_telemetry.update,
                should_stop=should_stop,
            )
    return (
        model_output,
        turn_stream_sink.streamed_content,
        model_telemetry,
    )
