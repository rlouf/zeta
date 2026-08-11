"""wire-v0 envelope validation and canonical serialization.

Validation is hand-rolled so the rule tokens it reports are the ones
the conformance vectors in ``spec/vectors/envelopes`` pin
(``EnvelopeError.rule`` equals line 1 of the matching ``.reason.txt``).
Payload *schema* validation is the runtime's job against its own
registry; this module validates envelope shape only.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from zeta import addresses
from zeta.ids import event_idempotency_id

PROTOCOL_VERSION = 0
MAX_INLINE_PAYLOAD_BYTES = 64 * 1024

KINDS = frozenset(
    {
        "hello",
        "hello_ack",
        "event",
        "ack",
        "heartbeat",
        "error",
        "shutdown",
        "call",
        "call_result",
    }
)
RESERVED_KINDS = frozenset({"event_batch"})
ROLES = frozenset({"source", "tool", "provider"})
ERROR_CODES = frozenset(
    {"protocol", "schema", "internal", "unsupported_version", "unsupported"}
)

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


class EnvelopeError(ValueError):
    """An envelope violated a wire-v0 rule; `rule` names which one."""

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(f"{rule}: {message}")
        self.rule = rule


def canonical_json(value: Any) -> str:
    """Serialize per spec §2.1: sorted keys, compact, literal UTF-8."""
    return addresses.canonical_json_bytes(value).decode("utf-8")


def now_ts() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def envelope(
    kind: str, message_id: str, ts: str | None = None, **fields: Any
) -> dict[str, Any]:
    """Build a v0 envelope; kind-specific fields come in as keywords."""
    return {
        "v": PROTOCOL_VERSION,
        "kind": kind,
        "id": message_id,
        "ts": ts or now_ts(),
        **fields,
    }


def mint_event_id(event_type: str, payload: dict[str, Any]) -> str:
    """Return the runtime-owned idempotency identity for a wire event."""
    return event_idempotency_id(event_type, payload)


def validate_envelope(value: Any) -> dict[str, Any]:
    """Validate one parsed envelope; raise EnvelopeError on violation."""
    if not isinstance(value, dict):
        raise EnvelopeError("not_an_object", "an envelope must be a JSON object")
    try:
        canonical_json(value)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise EnvelopeError(
            "bad_canonical_value",
            "an envelope must contain only canonical identity values",
        ) from exc
    if "v" not in value:
        raise EnvelopeError("missing_field:v", "an envelope must carry `v`")
    if not _is_int(value["v"]) or value["v"] < 0:
        raise EnvelopeError("bad_version", "`v` must be a non-negative integer")
    kind = value.get("kind")
    if kind is None:
        raise EnvelopeError("missing_field:kind", "an envelope must carry `kind`")
    if not isinstance(kind, str):
        raise EnvelopeError("bad_kind", "`kind` must be a string")
    message_id = value.get("id")
    if message_id is None:
        raise EnvelopeError("missing_field:id", "an envelope must carry `id`")
    if not isinstance(message_id, str) or not message_id:
        raise EnvelopeError("bad_id", "`id` must be a non-empty string")
    ts = value.get("ts")
    if ts is None:
        raise EnvelopeError("missing_field:ts", "an envelope must carry `ts`")
    _validate_ts(ts)
    if kind in RESERVED_KINDS:
        raise EnvelopeError("reserved_kind", f"kind {kind!r} is reserved")
    if kind not in KINDS:
        raise EnvelopeError("unknown_kind", f"unknown kind {kind!r}")
    _KIND_VALIDATORS[kind](value)
    return value


def _validate_ts(ts: Any) -> None:
    if not isinstance(ts, str) or not TS_RE.match(ts):
        raise EnvelopeError(
            "bad_timestamp", "`ts` must be RFC 3339 UTC with the Z designator"
        )
    try:
        datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EnvelopeError("bad_timestamp", str(exc)) from exc


def _validate_hello(value: dict) -> None:
    _required_string(value, "name")
    _required_string(value, "plugin_version")
    role = _required_string(value, "role")
    if role not in ROLES:
        raise EnvelopeError("bad_role", f"unknown role {role!r}")
    versions = value.get("protocol_versions")
    if versions is None:
        raise EnvelopeError(
            "missing_field:protocol_versions",
            "a hello must carry `protocol_versions`",
        )
    if (
        not isinstance(versions, list)
        or not versions
        or not all(_is_int(item) and item >= 0 for item in versions)
    ):
        raise EnvelopeError(
            "bad_protocol_versions",
            "`protocol_versions` must be a non-empty array of non-negative integers",
        )
    if role == "source":
        _validate_event_types(value)
    operations = value.get("operations")
    if operations is not None and (
        not isinstance(operations, list)
        or not all(
            isinstance(entry, dict)
            and isinstance(entry.get("name"), str)
            and entry["name"]
            for entry in operations
        )
    ):
        raise EnvelopeError(
            "bad_operations",
            "`operations` must be an array of {name} objects",
        )
    capabilities = value.get("capabilities")
    if capabilities is not None and not isinstance(capabilities, dict):
        raise EnvelopeError("bad_capabilities", "`capabilities` must be an object")
    heartbeat = value.get("heartbeat_secs")
    if heartbeat is not None and (
        not isinstance(heartbeat, int | float)
        or isinstance(heartbeat, bool)
        or not 1 <= heartbeat <= 300
    ):
        raise EnvelopeError(
            "bad_heartbeat_secs", "`heartbeat_secs` must be a number in [1, 300]"
        )
    window = value.get("ack_window")
    if window is not None and (not _is_int(window) or not 1 <= window <= 1024):
        raise EnvelopeError(
            "bad_ack_window", "`ack_window` must be an integer in [1, 1024]"
        )


def _validate_event_types(value: dict) -> None:
    event_types = value.get("event_types")
    if event_types is None:
        raise EnvelopeError(
            "missing_field:event_types", "a source hello must carry `event_types`"
        )
    if not isinstance(event_types, list) or not all(
        isinstance(entry, dict)
        and isinstance(entry.get("type"), str)
        and entry["type"]
        and isinstance(entry.get("schema"), str)
        and entry["schema"]
        for entry in event_types
    ):
        raise EnvelopeError(
            "bad_event_types",
            "`event_types` must be an array of {type, schema} string pairs",
        )


def _validate_hello_ack(value: dict) -> None:
    version = value.get("protocol_version")
    if version is None:
        raise EnvelopeError(
            "missing_field:protocol_version",
            "a hello_ack must carry `protocol_version`",
        )
    if not _is_int(version) or version < 0:
        raise EnvelopeError(
            "bad_protocol_version",
            "`protocol_version` must be a non-negative integer",
        )
    _required_string(value, "runtime")
    config = value.get("config")
    if config is not None and not isinstance(config, dict):
        raise EnvelopeError("bad_config", "`config` must be an object")


def _validate_call(value: dict) -> None:
    _required_string(value, "name")
    _required_string(value, "effect_key")
    payload = value.get("payload")
    if payload is None:
        raise EnvelopeError("missing_field:payload", "a call must carry `payload`")
    if not isinstance(payload, dict):
        raise EnvelopeError("bad_payload", "`payload` must be an object")


def _validate_call_result(value: dict) -> None:
    _required_string(value, "call_id")
    ok = value.get("ok")
    if ok is None:
        raise EnvelopeError("missing_field:ok", "a call_result must carry `ok`")
    if not isinstance(ok, bool):
        raise EnvelopeError("bad_ok", "`ok` must be a boolean")
    has_result = isinstance(value.get("result"), dict)
    has_error = isinstance(value.get("error"), dict)
    if ok and not has_result:
        raise EnvelopeError(
            "result_choice", "a successful call_result must carry a `result` object"
        )
    if not ok:
        error = value.get("error")
        if not has_error or not isinstance(error, dict):
            raise EnvelopeError(
                "result_choice", "a failed call_result must carry an `error` object"
            )
        if (
            not isinstance(error.get("code"), str)
            or not isinstance(error.get("message"), str)
            or not isinstance(error.get("retryable"), bool)
        ):
            raise EnvelopeError(
                "bad_error", "`error` must carry {code, message, retryable}"
            )


def _validate_event(value: dict) -> None:
    _required_string(value, "type")
    _required_string(value, "schema")
    for field in ("caused_by", "session_id"):
        if field not in value:
            raise EnvelopeError(
                f"missing_field:{field}",
                f"an event must carry `{field}` (null is allowed)",
            )
        if value[field] is not None and not isinstance(value[field], str):
            raise EnvelopeError(f"bad_{field}", f"`{field}` must be a string or null")
    payload = value.get("payload")
    payload_hash = value.get("payload_hash")
    has_payload = payload is not None
    has_hash = payload_hash is not None
    if has_payload == has_hash:
        raise EnvelopeError(
            "payload_choice",
            "an event must carry exactly one of `payload` and `payload_hash`",
        )
    if has_payload:
        if not isinstance(payload, dict):
            raise EnvelopeError("bad_payload", "`payload` must be an object")
        if len(canonical_json(payload).encode()) > MAX_INLINE_PAYLOAD_BYTES:
            raise EnvelopeError(
                "payload_too_large",
                "inline payloads are limited to 64 KiB; use `payload_hash`",
            )
    else:
        if not isinstance(payload_hash, str) or not addresses.is_b3(payload_hash):
            raise EnvelopeError(
                "bad_payload_hash",
                "`payload_hash` must be `b3:` plus 64 lowercase hex characters",
            )


def _validate_ack(value: dict) -> None:
    event_id = value.get("event_id")
    if event_id is None:
        raise EnvelopeError("missing_field:event_id", "an ack must carry `event_id`")
    if not isinstance(event_id, str) or not event_id:
        raise EnvelopeError("bad_event_id", "`event_id` must be a non-empty string")


def _validate_error(value: dict) -> None:
    _required_string(value, "code")
    _required_string(value, "message")
    retryable = value.get("retryable")
    if retryable is None:
        raise EnvelopeError(
            "missing_field:retryable", "an error must carry `retryable`"
        )
    if not isinstance(retryable, bool):
        raise EnvelopeError("bad_retryable", "`retryable` must be a boolean")


def _validate_heartbeat(value: dict) -> None:
    return None


def _validate_shutdown(value: dict) -> None:
    reason = value.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise EnvelopeError("bad_reason", "`reason` must be a string")


def _required_string(value: dict, field: str) -> str:
    item = value.get(field)
    if item is None:
        raise EnvelopeError(
            f"missing_field:{field}",
            f"a {value.get('kind', 'message')} must carry `{field}`",
        )
    if not isinstance(item, str) or not item:
        raise EnvelopeError(f"bad_{field}", f"`{field}` must be a non-empty string")
    return item


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


_KIND_VALIDATORS = {
    "hello": _validate_hello,
    "hello_ack": _validate_hello_ack,
    "event": _validate_event,
    "ack": _validate_ack,
    "heartbeat": _validate_heartbeat,
    "error": _validate_error,
    "shutdown": _validate_shutdown,
    "call": _validate_call,
    "call_result": _validate_call_result,
}
