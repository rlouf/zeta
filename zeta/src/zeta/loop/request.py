"""Immutable inputs and mutable state for one run.

The harness hands the loop a request and a set of dependencies. Neither
carries queue state or retry authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from zeta.capabilities.executors import ToolExecutor
from zeta.capabilities.registry import (
    CapabilityRegistry,
)
from zeta.context.builder import (
    PromptBuilder,
)
from zeta.loop.cancellation import (
    AbortReason,
)
from zeta.loop.config import AgentConfig
from zeta.loop.gateway import ModelGateway
from zeta.loop.types import AgentEventSink
from zeta.models import DefaultModelGateway
from zeta.substrate import Store


@dataclass(frozen=True)
class AgentRunRequest:
    """Durable request envelope shared by session and authored-agent runs."""

    objective: str
    workflow: str
    runtime: str
    tools: tuple[str, ...]
    context: str
    config: AgentConfig
    fresh: bool = False


@dataclass(frozen=True)
class RunDependencies:
    event_sink: AgentEventSink | None
    trace_store: Store | None
    tool_registry: CapabilityRegistry
    builder: PromptBuilder
    abort_reason: AbortReason
    tool_executor: ToolExecutor | None = None
    model_gateway: ModelGateway = field(default_factory=DefaultModelGateway)


def silent_run_dependencies(ctx: RunDependencies) -> RunDependencies:
    return replace(ctx, event_sink=None)
