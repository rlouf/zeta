"""Tool-call trace reporting helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from zeta.substrate import Object, ObjectId, SqliteStore, Store

from ..display.summarize import truncate


def tool_failure_detail(row: dict[str, Any]) -> str:
    """Return the most useful stored failure reason for CLI listings."""
    if row.get("ok") is not False:
        return ""
    error = row.get("error")
    if isinstance(error, dict) and error:
        return f" · {error.get('code')}: {error.get('message')}"
    result = row.get("result")
    if not isinstance(result, dict):
        return ""
    content = result.get("content")
    text = failure_text_content(row, content)
    metadata = result.get("metadata")
    status = metadata.get("status") if isinstance(metadata, dict) else None
    if isinstance(status, int):
        label = f"exit {status}" if row.get("name") == "bash" else f"status {status}"
        return f" · {label}" + (f": {truncate(text, 180)}" if text else "")
    if text:
        return f" · {truncate(text, 180)}"
    return ""


def first_text_content(content: object) -> str:
    text = raw_text_content(content)
    return " ".join(text.strip().split()) if text else ""


def raw_text_content(content: object) -> str:
    if not isinstance(content, list):
        return ""
    for item in content:
        if not isinstance(item, Mapping):
            continue
        content_item = cast("Mapping[str, object]", item)
        text = content_item.get("text")
        if isinstance(text, str) and text.strip():
            return text
    return ""


def failure_text_content(row: dict[str, Any], content: object) -> str:
    text = raw_text_content(content)
    if row.get("name") != "bash" or not text:
        return " ".join(text.strip().split()) if text else ""
    summary = bash_failure_summary(text) or text
    return " ".join(summary.strip().split())


def bash_failure_summary(text: str) -> str:
    markers = (
        "error:",
        "Error:",
        "Exception:",
        "exceptions.",
        "TimeoutError:",
        "Unexpected",
        "No such file",
        "not found",
        "/bin/sh:",
    )
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("raise "):
            continue
        if any(marker in stripped for marker in markers):
            return stripped
    return ""


def tool_call_rows(
    store: Store,
    *,
    session: str | None,
    failed: bool,
    successful: bool,
    limit: int,
) -> list[dict[str, Any]]:
    results = tool_results_by_call_id(store)
    rows: list[dict[str, Any]] = []
    for call_object_id, call in store.objects(("tool_call",), 10_000):
        row = tool_call_row(
            session=session,
            call_object_id=call_object_id,
            call=call,
            result_record=results.get(tool_call_id(call)),
        )
        row["created_at"] = tool_row_created_at(
            store,
            result_object_id=row.get("tool_result_object_id"),
            call_object_id=call_object_id,
        )
        if failed and row.get("ok") is not False:
            continue
        if successful and row.get("ok") is not True:
            continue
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def tool_results_by_call_id(
    store: Store,
) -> dict[str, tuple[ObjectId, Object]]:
    results: dict[str, tuple[ObjectId, Object]] = {}
    for result_object_id, result in store.objects(("tool_result",), 10_000):
        call_id = tool_call_id(result)
        if call_id and call_id not in results:
            results[call_id] = (result_object_id, result)
    return results


def tool_call_row(
    *,
    session: str | None,
    call_object_id: ObjectId,
    call: Object,
    result_record: tuple[ObjectId, Object] | None,
) -> dict[str, Any]:
    call_data = object_data(call)
    result_object_id, result_data, result_payload = result_fields(result_record)
    row = base_tool_call_row(
        session=session,
        call_object_id=call_object_id,
        call=call,
        call_data=call_data,
        result_object_id=result_object_id,
        result_payload=result_payload,
        name=str(result_data.get("name") or call_data.get("name") or ""),
    )
    if result_payload is not None:
        attach_tool_result(row, result_payload)
    return row


def object_data(obj: Object) -> dict[str, Any]:
    return obj.data if isinstance(obj.data, dict) else {}


def result_fields(
    result_record: tuple[ObjectId, Object] | None,
) -> tuple[ObjectId | None, dict[str, Any], dict[str, Any] | None]:
    if result_record is None:
        return None, {}, None
    result_object_id, result = result_record
    result_data = object_data(result)
    payload = result_data.get("result")
    result_payload = payload if isinstance(payload, dict) else None
    return result_object_id, result_data, result_payload


def base_tool_call_row(
    *,
    session: str | None,
    call_object_id: ObjectId,
    call: Object,
    call_data: dict[str, Any],
    result_object_id: ObjectId | None,
    result_payload: dict[str, Any] | None,
    name: str,
) -> dict[str, Any]:
    return {
        "session": session,
        "tool_call_id": tool_call_id(call),
        "name": name,
        "input": call_data.get("input")
        if isinstance(call_data.get("input"), dict)
        else {},
        "ok": result_payload.get("ok") if result_payload is not None else None,
        "tool_call_object_id": call_object_id,
        "tool_result_object_id": result_object_id,
    }


def attach_tool_result(row: dict[str, Any], result_payload: dict[str, Any]) -> None:
    row["result"] = result_payload
    error = result_payload.get("error")
    if isinstance(error, dict):
        row["error"] = error
        return
    recovered_error = recovered_tool_error(row)
    if recovered_error is not None:
        row["error"] = recovered_error


def recovered_tool_error(row: dict[str, Any]) -> dict[str, str] | None:
    if row.get("ok") is not False:
        return None
    result = row.get("result")
    if not isinstance(result, dict):
        return None
    message = failure_text_content(row, result.get("content"))
    metadata = result.get("metadata")
    status = metadata.get("status") if isinstance(metadata, dict) else None
    if not message and isinstance(status, int):
        message = (
            f"exit status {status}" if row.get("name") == "bash" else f"status {status}"
        )
    if not message:
        return None
    name = row.get("name")
    return {
        "code": f"{name or 'tool'}-failed",
        "message": message,
    }


def tool_call_id(obj: Object) -> str:
    data = obj.data if isinstance(obj.data, dict) else {}
    return str(data.get("tool_call_id") or "")


def tool_row_created_at(
    store: Store,
    *,
    result_object_id: Any,
    call_object_id: ObjectId,
) -> float | None:
    if not isinstance(store, SqliteStore):
        return None
    object_id_value = result_object_id if isinstance(result_object_id, str) else None
    records = store.derivation_records_for_output(object_id_value or call_object_id)
    if not records and object_id_value is not None:
        records = store.derivation_records_for_output(call_object_id)
    if not records:
        return None
    return max(float(record["created_at"]) for record in records)
