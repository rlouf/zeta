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
from zeta.events import DraftEvent
from zeta.loop.cancellation import (
    AbortReason,
)
from zeta.loop.config import AgentConfig
from zeta.loop.gateway import ModelGateway
from zeta.loop.types import AgentEventSink
from zeta.models import DefaultModelGateway
from zeta.substrate import Store
from zeta.trace.provenance import project_prompt_trace_projection


@dataclass(frozen=True)
class AgentRunRequest:
    """Durable request envelope shared by session and authored-agent runs."""

    objective: str
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
    tool_executor: ToolExecutor
    builder: PromptBuilder
    abort_reason: AbortReason
    model_gateway: ModelGateway = field(default_factory=DefaultModelGateway)


def silent_run_dependencies(ctx: RunDependencies) -> RunDependencies:
    return replace(ctx, event_sink=None)


def record_runtime_event(
    events: list[DraftEvent],
    draft: DraftEvent,
    *,
    ctx: RunDependencies,
) -> DraftEvent:
    events.append(draft)
    if ctx.event_sink is not None:
        ctx.event_sink(draft)
    if ctx.event_sink is None:
        project_prompt_trace_projection(events, ctx.builder.store())
    return draft
