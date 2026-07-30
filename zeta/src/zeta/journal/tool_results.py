"""Tool result normalisation for durable records.

A capability may fail without returning a structured error. These helpers give
every failure a code and a message, so a stored tool result reads the same way
whatever produced it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from zeta.journal.types import REFUSED_TOOL_ERROR_CODES


def tool_result_status(result: Mapping[str, Any]) -> str:
    if result.get("ok") is True:
        return "completed"
    error = result.get("error")
    if isinstance(error, dict) and error.get("code") in REFUSED_TOOL_ERROR_CODES:
        return "refused"
    return "failed"


def normalized_tool_result(name: str, result: Mapping[str, Any]) -> dict[str, Any]:
    stored = dict(result)
    if stored.get("ok") is not False or isinstance(stored.get("error"), dict):
        return stored
    message = tool_failure_message(stored)
    if message:
        stored["error"] = {
            "code": f"{name or 'tool'}-failed",
            "message": message,
        }
    return stored


def tool_failure_message(result: Mapping[str, Any]) -> str:
    content = result.get("content")
    text = first_tool_text(content)
    if text:
        return flatten_tool_text(text)
    metadata = result.get("metadata")
    if isinstance(metadata, dict):
        status = metadata.get("status")
        if isinstance(status, int):
            return f"status {status}"
    return ""


def first_tool_text(content: object) -> str:
    if not isinstance(content, list):
        return ""
    for item in content:
        if not isinstance(item, dict):
            continue
        text = cast("dict[str, Any]", item).get("text")
        if isinstance(text, str) and text.strip():
            return text
    return ""


def flatten_tool_text(text: str) -> str:
    return " ".join(text.strip().split())
