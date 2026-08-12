"""JSON-RPC message shapes shared by IPC peers and the runtime."""

from __future__ import annotations

import json
import math
from typing import Any, Literal

JSONRPC_VERSION = "2.0"
PROTOCOL_VERSION = 0
MAX_INLINE_PAYLOAD_BYTES = 64 * 1024

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
SERVER_ERROR = -32000

ROLES = frozenset({"source", "client", "provider"})
FIXED_REQUESTS = frozenset(
    {
        "events.publish",
        "events.list",
        "session.start",
        "session.send",
        "session.status",
        "session.list",
        "session.cancel",
        "ping",
        "shutdown",
    }
)

RequestId = str | int
MessageKind = Literal["request", "notification", "success", "error"]


class MessageError(ValueError):
    """A message violates the JSON-RPC profile and carries its response code."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def request(
    message_id: RequestId,
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one JSON-RPC request with object parameters."""
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": message_id,
        "method": method,
        "params": dict(params or {}),
    }


def notification(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build one JSON-RPC notification."""
    return {
        "jsonrpc": JSONRPC_VERSION,
        "method": method,
        "params": dict(params or {}),
    }


def success_response(message_id: RequestId, result: Any) -> dict[str, Any]:
    """Build a successful JSON-RPC response."""
    return {"jsonrpc": JSONRPC_VERSION, "id": message_id, "result": result}


def error_response(
    message_id: RequestId | None,
    code: int,
    message: str,
    data: Any | None = None,
) -> dict[str, Any]:
    """Build a JSON-RPC error response."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": message_id,
        "error": error,
    }


def validate_message(value: Any) -> dict[str, Any]:
    """Validate one parsed message and return it unchanged."""
    message = parse_message(value)
    kind = message_kind(message)
    if kind == "request":
        method = message["method"]
        params = _params(message)
        if method == "initialize":
            validate_initialize_params(params)
        elif method in FIXED_REQUESTS:
            _validate_fixed_request(method, params)
        else:
            _validate_provider_params(params)
    elif kind == "notification" and message["method"] == "event":
        _validate_event_notification(_params(message))
    return message


def parse_message(value: Any) -> dict[str, Any]:
    """Validate and classify the JSON-RPC shape without applying session rules."""
    if not isinstance(value, dict):
        raise MessageError(INVALID_REQUEST, "a JSON-RPC message must be an object")
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise MessageError(
            INVALID_REQUEST, "a message must contain JSON values"
        ) from exc
    if value.get("jsonrpc") != JSONRPC_VERSION:
        raise MessageError(
            INVALID_REQUEST, "a JSON-RPC message must carry `jsonrpc` equal to `2.0`"
        )
    has_method = "method" in value
    has_result = "result" in value
    has_error = "error" in value
    if sum((has_method, has_result, has_error)) != 1:
        raise MessageError(
            INVALID_REQUEST,
            "a message must be exactly one request, notification, success, or error",
        )
    if has_method:
        _parse_call(value)
    elif has_result:
        if "id" not in value:
            raise MessageError(INVALID_REQUEST, "a successful response must carry `id`")
        _validate_request_id(value["id"])
    else:
        _parse_error_response(value)
    return value


def message_kind(message: dict[str, Any]) -> MessageKind:
    """Return the JSON-RPC message class selected by its discriminant member."""
    if "method" in message:
        return "request" if "id" in message else "notification"
    return "success" if "result" in message else "error"


def validate_initialize_params(params: dict[str, Any]) -> dict[str, Any]:
    """Validate the strict peer initialization parameters."""
    _validate_keys(
        params,
        {
            "protocol_versions",
            "peer",
            "roles",
            "event_types",
            "methods",
            "heartbeat_seconds",
            "max_in_flight",
        },
        {"protocol_versions", "peer", "roles"},
    )
    versions = params.get("protocol_versions")
    if (
        not isinstance(versions, list)
        or not versions
        or not all(_is_u64(version) for version in versions)
        or len(set(versions)) != len(versions)
    ):
        raise MessageError(
            INVALID_PARAMS,
            "`protocol_versions` must contain unique non-negative integers",
        )
    peer = params.get("peer")
    if not isinstance(peer, dict):
        raise MessageError(INVALID_PARAMS, "`peer` must be an object")
    _validate_keys(peer, {"name", "version"}, {"name", "version"})
    _required_string(peer, "name")
    _required_string(peer, "version")
    roles = params.get("roles")
    if (
        not isinstance(roles, list)
        or not roles
        or not all(isinstance(role, str) and role in ROLES for role in roles)
        or len(set(roles)) != len(roles)
    ):
        raise MessageError(INVALID_PARAMS, "`roles` must contain unique known roles")
    _validate_event_types(params, source="source" in roles)
    _validate_methods(params, provider="provider" in roles)
    heartbeat = params.get("heartbeat_seconds")
    if heartbeat is not None and (
        isinstance(heartbeat, bool)
        or not isinstance(heartbeat, int | float)
        or not math.isfinite(heartbeat)
        or not 1 <= heartbeat <= 300
    ):
        raise MessageError(
            INVALID_PARAMS, "`heartbeat_seconds` must be a number in [1, 300]"
        )
    maximum = params.get("max_in_flight")
    if maximum is not None and (not _is_int(maximum) or not 1 <= maximum <= 1024):
        raise MessageError(
            INVALID_PARAMS, "`max_in_flight` must be an integer in [1, 1024]"
        )
    return params


def validate_initialize_result(
    result: Any, expected_roles: list[str]
) -> dict[str, Any]:
    """Validate the runtime result before a peer starts ordinary traffic."""
    if not isinstance(result, dict):
        raise MessageError(INVALID_REQUEST, "initialize result must be an object")
    _validate_keys(
        result,
        {
            "protocol_version",
            "runtime",
            "roles",
            "config",
            "heartbeat_seconds",
            "max_in_flight",
        },
        {
            "protocol_version",
            "runtime",
            "roles",
            "config",
            "heartbeat_seconds",
            "max_in_flight",
        },
        code=INVALID_REQUEST,
    )
    if result["protocol_version"] != PROTOCOL_VERSION:
        raise MessageError(INVALID_REQUEST, "runtime selected an unsupported version")
    runtime = result["runtime"]
    if not isinstance(runtime, dict):
        raise MessageError(INVALID_REQUEST, "`runtime` must be an object")
    _validate_keys(
        runtime,
        {"name", "version"},
        {"name", "version"},
        code=INVALID_REQUEST,
    )
    _required_string(runtime, "name", code=INVALID_REQUEST)
    _required_string(runtime, "version", code=INVALID_REQUEST)
    roles = result["roles"]
    if (
        not isinstance(roles, list)
        or not all(isinstance(role, str) and role in ROLES for role in roles)
        or len(roles) != len(set(roles))
        or set(roles) != set(expected_roles)
    ):
        raise MessageError(INVALID_REQUEST, "runtime must accept the requested roles")
    if not isinstance(result["config"], dict):
        raise MessageError(INVALID_REQUEST, "`config` must be an object")
    heartbeat = result["heartbeat_seconds"]
    if (
        isinstance(heartbeat, bool)
        or not isinstance(heartbeat, int | float)
        or not math.isfinite(heartbeat)
        or not 1 <= heartbeat <= 300
    ):
        raise MessageError(INVALID_REQUEST, "runtime returned an invalid heartbeat")
    maximum = result["max_in_flight"]
    if not _is_int(maximum) or not 1 <= maximum <= 1024:
        raise MessageError(INVALID_REQUEST, "runtime returned an invalid request limit")
    return result


def validate_publish_result(result: Any) -> dict[str, Any]:
    """Validate the durable boundary before a source advances its cursor."""
    if not isinstance(result, dict):
        raise MessageError(INVALID_REQUEST, "publish result must be an object")
    if not isinstance(result.get("inserted"), bool):
        raise MessageError(INVALID_REQUEST, "publish result requires `inserted`")
    if "event" not in result:
        raise MessageError(INVALID_REQUEST, "publish result requires `event`")
    _validate_durable_event(result["event"])
    return result


def method_is_reserved(method: str) -> bool:
    """Return whether a direct provider may not claim this method name."""
    return (
        method in {"initialize", "event", "ping", "shutdown"}
        or method.startswith("events.")
        or method.startswith("session.")
        or method.startswith("rpc.")
    )


def compact_json_bytes(value: Any) -> bytes:
    """Encode non-identity JSON with the protocol's compact writer form."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _parse_call(message: dict[str, Any]) -> None:
    method = message.get("method")
    if not isinstance(method, str) or not method:
        raise MessageError(INVALID_REQUEST, "`method` must be a non-empty string")
    _params(message)
    if "id" in message:
        _validate_request_id(message["id"])


def _parse_error_response(message: dict[str, Any]) -> None:
    if "id" not in message:
        raise MessageError(INVALID_REQUEST, "an error response must carry `id`")
    if message["id"] is not None:
        _validate_request_id(message["id"])
    error = message.get("error")
    if not isinstance(error, dict):
        raise MessageError(INVALID_REQUEST, "`error` must be an object")
    code = error.get("code")
    if not _is_i64(code):
        raise MessageError(INVALID_REQUEST, "`error.code` must be an i64 integer")
    if not isinstance(error.get("message"), str):
        raise MessageError(INVALID_REQUEST, "`error.message` must be a string")


def _params(message: dict[str, Any]) -> dict[str, Any]:
    params = message.get("params", {})
    if not isinstance(params, dict):
        raise MessageError(INVALID_REQUEST, "`params` must be an object")
    return params


def _validate_request_id(message_id: Any) -> None:
    if isinstance(message_id, str):
        return
    if not _is_int(message_id) or not -(2**63) <= message_id <= 2**64 - 1:
        raise MessageError(
            INVALID_REQUEST, "a request id must be a string or integral i64/u64 number"
        )


def _validate_event_types(params: dict[str, Any], *, source: bool) -> None:
    event_types = params.get("event_types")
    if source and event_types is None:
        raise MessageError(INVALID_PARAMS, "the source role requires `event_types`")
    if not source and event_types is not None:
        raise MessageError(INVALID_PARAMS, "`event_types` requires the source role")
    if event_types is None:
        return
    if not isinstance(event_types, list):
        raise MessageError(INVALID_PARAMS, "`event_types` must be an array")
    names: list[str] = []
    for declaration in event_types:
        if not isinstance(declaration, dict):
            raise MessageError(INVALID_PARAMS, "an event declaration must be an object")
        _validate_keys(declaration, {"type", "schema"}, {"type", "schema"})
        names.append(_required_string(declaration, "type"))
        _required_string(declaration, "schema")
    if len(names) != len(set(names)):
        raise MessageError(INVALID_PARAMS, "event types must not repeat")


def _validate_methods(params: dict[str, Any], *, provider: bool) -> None:
    methods = params.get("methods")
    if provider and methods is None:
        raise MessageError(INVALID_PARAMS, "the provider role requires `methods`")
    if not provider and methods is not None:
        raise MessageError(INVALID_PARAMS, "`methods` requires the provider role")
    if methods is None:
        return
    if not isinstance(methods, list):
        raise MessageError(INVALID_PARAMS, "`methods` must be an array")
    names: list[str] = []
    for declaration in methods:
        if not isinstance(declaration, dict):
            raise MessageError(INVALID_PARAMS, "a method declaration must be an object")
        _validate_keys(declaration, {"name"}, {"name"})
        name = _required_string(declaration, "name")
        if method_is_reserved(name):
            raise MessageError(INVALID_PARAMS, f"method {name!r} is reserved")
        names.append(name)
    if len(names) != len(set(names)):
        raise MessageError(INVALID_PARAMS, "method names must not repeat")


def _validate_fixed_request(method: str, params: dict[str, Any]) -> None:
    validators = {
        "events.publish": _validate_publish,
        "events.list": _validate_events_list,
        "session.start": _validate_session_start,
        "session.send": _validate_session_send,
        "session.status": _validate_session_status,
        "session.list": _validate_empty,
        "session.cancel": _validate_session_cancel,
        "ping": _validate_empty,
        "shutdown": _validate_shutdown,
    }
    validators[method](params)


def _validate_publish(params: dict[str, Any]) -> None:
    optional = {"idempotency_key", "caused_by", "session_id", "run_id", "turn_id"}
    _validate_keys(params, {"type", "payload", *optional}, {"type", "payload"})
    _required_string(params, "type")
    payload = params["payload"]
    if not isinstance(payload, dict):
        raise MessageError(INVALID_PARAMS, "`payload` must be an object")
    if len(compact_json_bytes(payload)) > MAX_INLINE_PAYLOAD_BYTES:
        raise MessageError(INVALID_PARAMS, "`payload` exceeds the 64 KiB limit")
    for field in optional:
        _optional_string(params, field)


def _validate_events_list(params: dict[str, Any]) -> None:
    strings = {
        "event_type",
        "event_type_prefix",
        "session_id",
        "run_id",
        "turn_id",
        "caused_by",
    }
    _validate_keys(params, strings | {"after_cursor", "limit", "newest_first"}, set())
    for field in strings:
        _optional_string(params, field)
    for field in ("after_cursor", "limit"):
        if field in params and not _is_u64(params[field]):
            raise MessageError(INVALID_PARAMS, f"`{field}` must be non-negative")
    if "newest_first" in params and not isinstance(params["newest_first"], bool):
        raise MessageError(INVALID_PARAMS, "`newest_first` must be a boolean")


def _validate_session_start(params: dict[str, Any]) -> None:
    _validate_keys(params, {"message", "idempotency_key"}, {"message"})
    _required_string(params, "message")
    _optional_string(params, "idempotency_key")


def _validate_session_send(params: dict[str, Any]) -> None:
    _validate_keys(
        params,
        {"session_id", "message", "idempotency_key"},
        {"session_id", "message"},
    )
    _required_string(params, "session_id")
    _required_string(params, "message")
    _optional_string(params, "idempotency_key")


def _validate_session_status(params: dict[str, Any]) -> None:
    _validate_keys(params, {"session_id"}, {"session_id"})
    _required_string(params, "session_id")


def _validate_session_cancel(params: dict[str, Any]) -> None:
    _validate_keys(params, {"run_id", "session_id", "reason"}, {"run_id"})
    _required_string(params, "run_id")
    _optional_string(params, "session_id")
    _optional_string(params, "reason")


def _validate_shutdown(params: dict[str, Any]) -> None:
    _validate_keys(params, {"reason"}, set())
    _optional_string(params, "reason")


def _validate_empty(params: dict[str, Any]) -> None:
    _validate_keys(params, set(), set())


def _validate_provider_params(params: dict[str, Any]) -> None:
    _validate_keys(params, {"input", "base_dir", "effect_key"}, {"input"})
    if not isinstance(params["input"], dict):
        raise MessageError(INVALID_PARAMS, "`input` must be an object")
    _optional_string(params, "base_dir")
    _optional_string(params, "effect_key")


def _validate_event_notification(params: dict[str, Any]) -> None:
    _validate_keys(params, {"event"}, {"event"})
    _validate_durable_event(params["event"])


def _validate_durable_event(event: Any) -> None:
    if not isinstance(event, dict):
        raise MessageError(INVALID_PARAMS, "`event` must be an object")
    required = {
        "id",
        "type",
        "source",
        "payload",
        "idempotency_key",
        "caused_by",
        "session_id",
        "run_id",
        "turn_id",
        "timestamp_ms",
        "cursor",
    }
    missing = required - event.keys()
    if missing:
        raise MessageError(INVALID_PARAMS, f"event is missing {sorted(missing)[0]!r}")
    for field in ("id", "type", "source"):
        _required_string(event, field)
    if not isinstance(event["payload"], dict):
        raise MessageError(INVALID_PARAMS, "`event.payload` must be an object")
    for field in ("idempotency_key", "caused_by", "session_id", "run_id", "turn_id"):
        _optional_string(event, field, required=True)
    for field in ("timestamp_ms", "cursor"):
        if not _is_u64(event[field]):
            raise MessageError(INVALID_PARAMS, f"`event.{field}` must be non-negative")


def _validate_keys(
    value: dict[str, Any],
    allowed: set[str],
    required: set[str],
    *,
    code: int = INVALID_PARAMS,
) -> None:
    unknown = value.keys() - allowed
    if unknown:
        raise MessageError(code, f"unsupported parameter {sorted(unknown)[0]!r}")
    missing = required - value.keys()
    if missing:
        raise MessageError(code, f"missing parameter {sorted(missing)[0]!r}")


def _required_string(
    value: dict[str, Any], field: str, *, code: int = INVALID_PARAMS
) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item:
        raise MessageError(code, f"`{field}` must be a non-empty string")
    return item


def _optional_string(
    value: dict[str, Any], field: str, *, required: bool = False
) -> None:
    if field not in value:
        if required:
            raise MessageError(INVALID_PARAMS, f"missing parameter {field!r}")
        return
    item = value[field]
    if item is not None and (not isinstance(item, str) or not item):
        raise MessageError(INVALID_PARAMS, f"`{field}` must be null or non-empty")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_i64(value: Any) -> bool:
    return _is_int(value) and -(2**63) <= value <= 2**63 - 1


def _is_u64(value: Any) -> bool:
    return _is_int(value) and 0 <= value <= 2**64 - 1
