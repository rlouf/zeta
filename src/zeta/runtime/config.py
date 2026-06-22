"""Runtime config and config adapters for Zeta turn execution."""

import os
from pathlib import Path

from zeta.agents.capabilities import AgentConfig
from zeta.capabilities.types import ExecutionMode
from zeta.runtime.requests import SessionRunParams


def zeta_state_dir() -> Path:
    root = os.environ.get("ZETA_STATE_DIR")
    return Path(root).expanduser() if root else Path.home() / ".zeta"


def session_agent_config(
    params: SessionRunParams,
    *,
    enabled_capabilities: tuple[str, ...],
    execution_mode: ExecutionMode,
    session_id: str,
) -> AgentConfig:
    return AgentConfig(
        system_prompt=params.system,
        allowed_capabilities=enabled_capabilities,
        max_turns=params.max_steps,
        stop_on_staged_effect=True,
        execution_mode=execution_mode,
        model_name=params.model,
        model_url=params.url,
        model_session_id=session_id,
        thinking=params.thinking,
        model_api=params.api,
        max_wall_seconds=params.max_wall_seconds,
    )
