"""Shared vocabulary for one run.

These aliases and defaults sit below the gateway, the request, and the steps,
so every part of the loop can name the same things.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from zeta.capabilities.registry import registry as _runtime_tool_registry
from zeta.events import DraftEvent, Event

AgentEventSink = Callable[[DraftEvent], None]
TimelineEvent = Event | dict[str, Any]
DEFAULT_MAX_TURNS = 25
MODEL_TIMELINE_TYPES = frozenset(
    {
        "user_message",
        "model",
        "model_usage",
        "tool_call",
        "tool_result",
        "turn_aborted",
    }
)
tool_registry = _runtime_tool_registry
time_monotonic = time.monotonic
