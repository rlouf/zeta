"""Model input and output domain shapes."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ModelInput:
    """A normalized model request produced by prompt assembly.

    `ModelInput` carries rendered chat messages, available tool descriptors,
    model selection hints, and generation options. Model gateways translate it
    into provider-specific HTTP or SDK requests.
    """

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
    """Token accounting reported with a model response.

    Model adapters build this from provider usage payloads. Runtime and display
    code use it for telemetry, context accounting, and trace records when a
    provider reports token counts.
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class ModelOutput:
    """A normalized assistant response returned by a model gateway.

    The runtime reads `message` for assistant text and tool calls, records
    `usage` and provider metadata as telemetry, and stores replay items in trace
    data when the provider exposes transport-specific response fragments.
    """

    message: dict[str, Any]
    finish_reason: str | None = None
    usage: ModelUsage | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    provider_replay_items: tuple[dict[str, Any], ...] = ()
