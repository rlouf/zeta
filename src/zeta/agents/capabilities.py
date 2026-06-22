"""Authored-agent capability declarations."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import TracebackType
from typing import Literal, Protocol

from zeta.capabilities.types import ExecutionMode

CompactionStrategy = Literal["structural_trim", "drop_oldest"]


class ModelStatus(Protocol):
    def __enter__(self) -> "ModelStatus": ...

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _traceback: TracebackType | None,
        /,
    ) -> bool: ...

    def reasoning_delta(self, text: str) -> None: ...


@dataclass(frozen=True)
class CompactionPolicy:
    """Select how model-facing working memory is bounded for one turn."""

    strategy: CompactionStrategy = "structural_trim"
    max_context_tokens: int | None = None


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
    compaction_policy: CompactionPolicy | None = None
    model_status_factory: Callable[[], ModelStatus] | None = None
