"""Runtime tool for authorized session history."""

from __future__ import annotations

from typing import Any

from zeta.capabilities.executors import CapabilityFunction
from zeta.capabilities.types import Capability, CapabilityId
from zeta.trace.query import QueryLogReader

QUERY_LOG_CAPABILITY_ID = "zeta.query_log"

QUERY_LOG_SPEC = Capability(
    CapabilityId("zeta", "query_log"),
    (
        "Query prior model runs in the current authorized session. Use it to "
        "recover earlier decisions, outcomes, tool activity, and prompt trace ids. "
        "Cite the returned run ids when relying on history."
    ),
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "since": {
                "type": "string",
                "description": (
                    "Only runs at or after YYYY-MM-DD, or an age like 2d, 6h, or 30m."
                ),
            },
            "failed": {
                "type": "boolean",
                "description": "Only failed or aborted runs.",
            },
            "run_id": {
                "type": "string",
                "description": "Expand one prior run by full id or unique id prefix.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": "Maximum number of prior runs to list.",
            },
        },
    },
)


def bind_history_tools(
    reader: QueryLogReader | None,
) -> dict[str, CapabilityFunction]:
    """Bind the authorized reader so history cannot cross a session boundary."""
    return {
        QUERY_LOG_CAPABILITY_ID: lambda params: query_log(params, reader=reader),
    }


def query_log(
    params: dict[str, Any],
    *,
    reader: QueryLogReader | None,
) -> dict[str, Any]:
    if reader is None:
        return query_log_unavailable(params)
    return reader(params)


def query_log_unavailable(_params: dict[str, Any]) -> dict[str, Any]:
    """Refuse history access when no durable session authorizes a reader."""
    return {
        "ok": False,
        "error": {
            "code": "query-log-unavailable",
            "message": "query_log is unavailable outside a durable runtime session",
        },
    }
