"""Deprecated compatibility imports for old timeline APIs.

Use `zeta.events`, `zeta.context`, or `zeta.substrate` for new code.
"""

from __future__ import annotations

from .context.components import (
    ChatMessageEntry,
    _chat_message_entries,
    chat_messages,
    from_message_boundary,
    renderable_tool_call,
    role_or_event_chat_message,
    tool_call_message,
    tool_result_event_from_message,
    tool_result_message,
)
from .events import (
    current_timeline,
    event_payload,
    event_time_value,
    record_event,
)
from .events import (
    event_reader_from_trace_store as event_reader_from_trace_store,
)
from .events import (
    exact_event_time as exact_event_time,
)
from .events import (
    last_event_time as last_event_time,
)
from .events import (
    latest_zeta_event_time as latest_zeta_event_time,
)
from .events import (
    timeline_event_from_durable_event as timeline_event_from_durable_event,
)
from .events import (
    timeline_from_events as timeline_from_events,
)
from .substrate import (
    add_event_link as add_event_link,
)
from .substrate import (
    trace_object_id as trace_object_id,
)

__all__ = [
    "ChatMessageEntry",
    "_chat_message_entries",
    "add_event_link",
    "chat_messages",
    "current_timeline",
    "event_payload",
    "event_reader_from_trace_store",
    "event_time_value",
    "exact_event_time",
    "from_message_boundary",
    "last_event_time",
    "latest_zeta_event_time",
    "record_event",
    "renderable_tool_call",
    "role_or_event_chat_message",
    "timeline_event_from_durable_event",
    "timeline_from_events",
    "tool_call_message",
    "tool_result_event_from_message",
    "tool_result_message",
    "trace_object_id",
]


def optional_event_str(value: object) -> str | None:
    return value if isinstance(value, str) else None
