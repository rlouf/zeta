"""Run results and step outcomes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from zeta.context.components import PromptTrace
from zeta.records.events import DraftEvent

StepName = Literal[
    "check_budget",
    "build_prompt",
    "call_model",
    "record_assistant",
    "record_capability_call",
    "execute_capability",
    "record_capability_result",
    "finish_run",
    "abort_run",
]


@dataclass(frozen=True)
class StepEffect:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StepResult:
    step: StepName
    effects: tuple[StepEffect, ...] = ()


@dataclass(frozen=True)
class AgentRunResult:
    final_answer: str = ""
    telemetry: dict[str, Any] = field(default_factory=dict)
    events: list[DraftEvent] = field(default_factory=list)
    staged_effect: dict[str, Any] | None = None
    answer_streamed: bool = False
    model_telemetry_calls: list[dict[str, Any]] = field(default_factory=list)
    prompt_traces: list[PromptTrace] = field(default_factory=list)
    steps: list[StepResult] = field(default_factory=list)


def agent_run_result_payload(result: AgentRunResult) -> dict[str, Any]:
    payload: dict[str, Any] = {"final_answer": result.final_answer}
    if result.events:
        payload["events"] = [asdict(event) for event in result.events]
    if result.staged_effect is not None:
        payload["staged_effect"] = result.staged_effect
    return payload


RunStopReason = Literal["finished", "staged_effect", "aborted", "max_turns"]
RunInfoKind = Literal["model", "tools", "stopped"]


@dataclass(frozen=True)
class RunInfo:
    kind: RunInfoKind
    appended_events: tuple[DraftEvent, ...] = ()
    prompt_trace: PromptTrace | None = None
    model_telemetry: dict[str, Any] = field(default_factory=dict)
    staged_effect: dict[str, Any] | None = None
    final_answer: str = ""
    answer_streamed: bool = False


@dataclass
class RunState:
    events: list[DraftEvent] = field(default_factory=list)
    pending_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    pending_model_telemetry: dict[str, Any] = field(default_factory=dict)
    pending_tool_parent_id: str | None = None
    latest_model_telemetry: dict[str, Any] = field(default_factory=dict)
    model_telemetry_calls: list[dict[str, Any]] = field(default_factory=list)
    prompt_traces: list[PromptTrace] = field(default_factory=list)
    steps: list[StepResult] = field(default_factory=list)
    next_model_caused_by: str | None = None
    turn: int = 0
    stop: RunStopReason | None = None

    def result(
        self,
        *,
        final_answer: str = "",
        staged_effect: dict[str, Any] | None = None,
        answer_streamed: bool = False,
    ) -> AgentRunResult:
        return AgentRunResult(
            final_answer=final_answer,
            events=self.events,
            staged_effect=staged_effect,
            answer_streamed=answer_streamed,
            telemetry=self.latest_model_telemetry,
            model_telemetry_calls=self.model_telemetry_calls,
            prompt_traces=self.prompt_traces,
            steps=self.steps,
        )

    def note_model_telemetry(self, model_telemetry: dict[str, Any]) -> None:
        if not model_telemetry:
            return
        self.latest_model_telemetry = model_telemetry
        self.model_telemetry_calls.append(model_telemetry)

    def note_prompt_trace(self, prompt_trace: PromptTrace | None) -> None:
        if prompt_trace is not None:
            self.prompt_traces.append(prompt_trace)

    def note_step(self, step: StepName, *effects: StepEffect) -> None:
        self.steps.append(StepResult(step, effects))
