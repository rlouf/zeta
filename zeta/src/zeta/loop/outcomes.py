"""Run results and step outcomes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from zeta.context.components import PromptTrace
from zeta.context.transforms import ContentPromotion
from zeta.events import DraftEvent
from zeta.tools.events import CancelRequest, PublishEventRequest, WaitRequest

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
class StepResult:
    step: StepName


@dataclass(frozen=True)
class AgentRunResult:
    final_answer: str = ""
    final_object_id: str | None = None
    stop_reason: RunStopReason | None = None
    telemetry: dict[str, Any] = field(default_factory=dict)
    events: list[DraftEvent] = field(default_factory=list)
    answer_streamed: bool = False
    model_telemetry_calls: list[dict[str, Any]] = field(default_factory=list)
    prompt_traces: list[PromptTrace] = field(default_factory=list)
    steps: list[StepResult] = field(default_factory=list)
    publish_event_requests: list[PublishEventRequest] = field(default_factory=list)
    wait_requests: list[WaitRequest] = field(default_factory=list)
    cancel_requests: list[CancelRequest] = field(default_factory=list)
    content_promotions: list[ContentPromotion] = field(default_factory=list)


def agent_run_result_payload(result: AgentRunResult) -> dict[str, Any]:
    payload: dict[str, Any] = {"final_answer": result.final_answer}
    if result.final_object_id is not None:
        payload["final_object_id"] = result.final_object_id
    if result.stop_reason is not None:
        payload["stop_reason"] = result.stop_reason
    if result.events:
        payload["events"] = [asdict(event) for event in result.events]
    if result.publish_event_requests:
        payload["publish_event_requests"] = [
            asdict(request) for request in result.publish_event_requests
        ]
    if result.wait_requests:
        payload["wait_requests"] = [asdict(request) for request in result.wait_requests]
    if result.cancel_requests:
        payload["cancel_requests"] = [
            asdict(request) for request in result.cancel_requests
        ]
    if result.content_promotions:
        payload["content_promotions"] = [
            asdict(request) for request in result.content_promotions
        ]
    return payload


RunStopReason = Literal["finished", "tool_stop", "aborted", "max_turns"]
RunInfoKind = Literal["model", "tools", "stopped"]


@dataclass(frozen=True)
class RunInfo:
    kind: RunInfoKind
    appended_events: tuple[DraftEvent, ...] = ()
    prompt_trace: PromptTrace | None = None
    model_telemetry: dict[str, Any] = field(default_factory=dict)
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
    publish_event_requests: list[PublishEventRequest] = field(default_factory=list)
    wait_requests: list[WaitRequest] = field(default_factory=list)
    cancel_requests: list[CancelRequest] = field(default_factory=list)
    content_promotions: list[ContentPromotion] = field(default_factory=list)
    final_object_id: str | None = None
    selected_final_answer: str = ""
    next_tool_position: int = 0

    def result(
        self,
        *,
        final_answer: str = "",
        answer_streamed: bool = False,
    ) -> AgentRunResult:
        return AgentRunResult(
            final_answer=self.selected_final_answer or final_answer,
            final_object_id=self.final_object_id,
            stop_reason=self.stop,
            events=self.events,
            answer_streamed=answer_streamed,
            telemetry=self.latest_model_telemetry,
            model_telemetry_calls=self.model_telemetry_calls,
            prompt_traces=self.prompt_traces,
            steps=self.steps,
            publish_event_requests=self.publish_event_requests,
            wait_requests=self.wait_requests,
            cancel_requests=self.cancel_requests,
            content_promotions=self.content_promotions,
        )

    def note_model_telemetry(self, model_telemetry: dict[str, Any]) -> None:
        if not model_telemetry:
            return
        self.latest_model_telemetry = model_telemetry
        self.model_telemetry_calls.append(model_telemetry)

    def note_prompt_trace(self, prompt_trace: PromptTrace | None) -> None:
        if prompt_trace is not None:
            self.prompt_traces.append(prompt_trace)

    def note_step(self, step: StepName) -> None:
        self.steps.append(StepResult(step))
