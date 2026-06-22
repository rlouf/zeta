"""Request parsing for Zeta session-run calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class SessionRequestError(ValueError):
    """Raised when a session-level request cannot be converted into a turn."""

    code: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)


SessionWorkflow = Literal["ask", "propose", "do"]


@dataclass(frozen=True)
class SessionRunParams:
    objective: str
    workflow: SessionWorkflow = "ask"
    tools: list[str] | None = None
    context: str = ""
    system: str | None = None
    model: str | None = None
    url: str | None = None
    thinking: str | None = None
    api: str | None = None
    max_steps: int | None = None
    max_wall_seconds: float | None = None

    def run_payload(self, run_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "objective": self.objective,
            "workflow": self.workflow,
            "runtime": "zeta-rpc",
            "run_id": run_id,
            "tools": list(self.tools or ()),
            "context": self.context,
        }
        for key in (
            "system",
            "model",
            "url",
            "thinking",
            "api",
            "max_steps",
            "max_wall_seconds",
        ):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        return payload


def session_run_params(params: dict[str, Any]) -> SessionRunParams:
    """Construct validated session run params without reviving mapping parser methods."""

    try:
        request = SessionRunParams(**params)
    except TypeError as exc:
        raise SessionRequestError(
            "invalid_params",
            f"SessionRunParams parameters are invalid: {exc}",
            {"message": f"SessionRunParams parameters are invalid: {exc}"},
        ) from exc
    if not request.objective:
        raise SessionRequestError(
            "missing_objective",
            "session.run requires objective",
            {"message": "session.run requires objective"},
        )
    if request.workflow not in {"ask", "propose", "do"}:
        raise SessionRequestError(
            "invalid_workflow",
            "workflow must be ask, propose, or do",
            {
                "message": "workflow must be ask, propose, or do",
                "workflow": request.workflow,
            },
        )
    if request.tools is not None:
        for tool in request.tools:
            if not isinstance(tool, str) or not tool:
                raise SessionRequestError(
                    "invalid_tools",
                    "tools must contain non-empty strings",
                    {"message": "tools must contain non-empty strings"},
                )
    return request
