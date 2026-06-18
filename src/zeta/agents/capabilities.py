"""Authored-agent capability declarations."""

from collections.abc import Iterable
from dataclasses import dataclass

from ..capabilities import ExecutionMode


@dataclass(frozen=True)
class AgentConfig:
    """Configuration for one Zeta turn."""

    system_prompt: str | None = None
    allowed_capabilities: Iterable[str] | None = None
    max_turns: int | None = None
    stop_on_staged_effect: bool = True
    execution_mode: ExecutionMode = "stage"
    model_profile: str | None = None
    model_name: str | None = None
    model_url: str | None = None
    model_session_id: str | None = None
    thinking: str | None = None
    model_api: str | None = None
    max_wall_seconds: float | None = None
