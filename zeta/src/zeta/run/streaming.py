"""Model stream sinks that record streaming deltas as runtime events."""

from __future__ import annotations

from collections.abc import Callable

from zeta.records.events import DraftEvent, status_update_draft, stream_chunk_draft
from zeta.run.config import ModelStatus

AgentEventSink = Callable[[DraftEvent], None]


class ModelTurnStreamSink:
    """Record model stream deltas as runtime events."""

    def __init__(
        self,
        events: list[DraftEvent],
        event_sink: AgentEventSink | None = None,
    ) -> None:
        self.events = events
        self.event_sink = event_sink
        self.streamed_content = False

    def content_delta(self, text: str) -> None:
        if not text:
            return
        self.streamed_content = True
        draft = stream_chunk_draft(text)
        self.events.append(draft)
        if self.event_sink is not None:
            self.event_sink(draft)

    def reasoning_delta(self, text: str) -> None:
        if not text:
            return
        draft = status_update_draft("reasoning_delta", text)
        self.events.append(draft)
        if self.event_sink is not None:
            self.event_sink(draft)


class StatusAwareModelStream:
    def __init__(self, stream: ModelTurnStreamSink, status: ModelStatus) -> None:
        self.stream = stream
        self.status = status

    @property
    def streamed_content(self) -> bool:
        return self.stream.streamed_content

    def content_delta(self, text: str) -> None:
        self.stream.content_delta(text)

    def reasoning_delta(self, text: str) -> None:
        self.status.reasoning_delta(text)
        self.stream.reasoning_delta(text)
