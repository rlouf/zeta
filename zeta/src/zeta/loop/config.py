"""Configuration for one Zeta run."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Protocol

from zeta.context.compaction import CompactionPolicy


class ModelStatus(Protocol):
    def __enter__(self) -> ModelStatus: ...

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _traceback: TracebackType | None,
        /,
    ) -> bool: ...

    def reasoning_delta(self, text: str) -> None: ...


@dataclass(frozen=True)
class AgentConfig:
    """Configuration for one Zeta turn."""

    system_prompt: str | None = None
    allowed_capabilities: Iterable[str] | None = None
    max_turns: int | None = None
    model_profile: str | None = None
    model_name: str | None = None
    model_url: str | None = None
    model_session_id: str | None = None
    thinking: str | None = None
    model_api: str | None = None
    tool_profile: str = "native"
    max_wall_seconds: float | None = None
    compaction_policy: CompactionPolicy | None = None
    model_status_factory: Callable[[], ModelStatus] | None = None
    base_dir: Path | None = None
    effect_scope: str | None = None
