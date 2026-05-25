"""Trust metadata for glyph outputs.

Sigil records where a value came from and what it is allowed to do. The goal is
to make future agentic glyphs compose without silently promoting web/model text
into executable or writable authority.
"""

from __future__ import annotations

from typing import Any, Literal, Sequence, cast

Integrity = Literal["human", "local_model", "local_file", "web", "unknown"]
Capability = Literal["none", "propose", "read", "write_boxed", "exec_boxed"]

INTEGRITY_ORDER: dict[Integrity, int] = {
    "unknown": 0,
    "web": 1,
    "local_file": 2,
    "local_model": 3,
    "human": 4,
}

CAPABILITY_ORDER: dict[Capability, int] = {
    "none": 0,
    "propose": 1,
    "read": 2,
    "write_boxed": 3,
    "exec_boxed": 4,
}

INTEGRITIES = frozenset(INTEGRITY_ORDER)
CAPABILITIES = frozenset(CAPABILITY_ORDER)


class SecurityViolation(ValueError):
    """Raised when a state transition would increase trust without consent."""

    pass


def normalize_integrity(value: object) -> Integrity:
    """Map arbitrary stored values into the known integrity lattice."""
    if isinstance(value, str) and value in INTEGRITIES:
        return cast(Integrity, value)
    return "unknown"


def normalize_capability(value: object) -> Capability:
    """Map arbitrary stored values into the known capability lattice."""
    if isinstance(value, str) and value in CAPABILITIES:
        return cast(Capability, value)
    return "none"


def normalize_taint(value: object, *, legacy: bool = False) -> list[str]:
    """Normalize taint labels so old or malformed state stays explicit."""
    if isinstance(value, list):
        taint = [str(item) for item in value if isinstance(item, str) and item]
    else:
        taint = []
    if legacy and not taint:
        taint = ["legacy"]
    return sorted(set(taint))


def normalize_inputs(value: object) -> list[str]:
    """Normalize provenance links to event IDs."""
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item]


def normalize_trust_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return a record with complete trust metadata fields."""
    legacy = "integrity" not in record and "taint" not in record
    normalized = dict(record)
    normalized["integrity"] = normalize_integrity(record.get("integrity"))
    normalized["capability"] = normalize_capability(record.get("capability"))
    normalized["taint"] = normalize_taint(record.get("taint"), legacy=legacy)
    normalized["inputs"] = normalize_inputs(record.get("inputs"))
    normalized["provisional"] = bool(record.get("provisional", False))
    return normalized


def min_integrity(records: Sequence[dict[str, Any]]) -> Integrity:
    """Return the lowest integrity among input records."""
    if not records:
        return "unknown"
    return min(
        (normalize_integrity(record.get("integrity")) for record in records),
        key=lambda integrity: INTEGRITY_ORDER[integrity],
    )


def cap_capability(requested: Capability, invocation_cap: Capability) -> Capability:
    """Prevent a composed operation from exceeding its invocation capability."""
    if CAPABILITY_ORDER[requested] <= CAPABILITY_ORDER[invocation_cap]:
        return requested
    return invocation_cap


def create_trust_metadata(
    *,
    glyph: str,
    integrity: Integrity,
    capability: Capability,
    taint: Sequence[str],
    inputs: Sequence[str] = (),
    input_records: Sequence[dict[str, Any]] = (),
    provisional: bool = False,
    fresh_human: bool = False,
) -> dict[str, Any]:
    """Create trust metadata for a freshly produced value.

    Fresh human input is the only path that may intentionally raise integrity.
    """
    normalized_inputs = [item for item in inputs if item]
    normalized_taint = sorted({item for item in taint if item})
    if input_records and not fresh_human:
        inherited = min_integrity(input_records)
        if INTEGRITY_ORDER[integrity] > INTEGRITY_ORDER[inherited]:
            raise SecurityViolation(
                f"integrity promotion requires fresh human input: {inherited} -> {integrity}"
            )
    return {
        "glyph": glyph,
        "inputs": normalized_inputs,
        "integrity": integrity,
        "capability": capability,
        "taint": normalized_taint,
        "provisional": provisional,
    }


def inherit_security(
    *,
    glyph: str,
    input_records: Sequence[dict[str, Any]],
    capability: Capability | None = None,
    extra_taint: Sequence[str] = (),
    provisional: bool | None = None,
) -> dict[str, Any]:
    """Derive trust metadata from prior records without increasing integrity."""
    normalized_inputs = [record_id(record) for record in input_records]
    normalized_inputs = [item for item in normalized_inputs if item]
    inherited = [normalize_trust_record(record) for record in input_records]
    taint = set(extra_taint)
    is_provisional = False
    for record in inherited:
        taint.update(record["taint"])
        is_provisional = is_provisional or bool(record.get("provisional"))
    return {
        "glyph": glyph,
        "inputs": normalized_inputs,
        "integrity": min_integrity(inherited),
        "capability": capability or min_capability(inherited),
        "taint": sorted(taint) or ["legacy"],
        "provisional": is_provisional if provisional is None else provisional,
    }


def min_capability(records: Sequence[dict[str, Any]]) -> Capability:
    """Return the lowest capability among input records."""
    if not records:
        return "none"
    return min(
        (normalize_capability(record.get("capability")) for record in records),
        key=lambda capability: CAPABILITY_ORDER[capability],
    )


def record_id(record: dict[str, Any]) -> str:
    """Return the stable event identifier used for provenance links."""
    value = record.get("event_id") or record.get("id")
    return str(value) if value else ""


def reject_promotion(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    fresh_human: bool = False,
) -> None:
    """Reject integrity promotion unless fresh human input justifies it."""
    before_integrity = normalize_integrity(before.get("integrity"))
    after_integrity = normalize_integrity(after.get("integrity"))
    if fresh_human:
        return
    if INTEGRITY_ORDER[after_integrity] > INTEGRITY_ORDER[before_integrity]:
        raise SecurityViolation(
            f"integrity promotion requires fresh human input: {before_integrity} -> {after_integrity}"
        )


def ensure_no_auto_run(metadata: dict[str, Any]) -> None:
    """Prevent web-tainted state from becoming an automatic execution source."""
    normalized = normalize_trust_record(metadata)
    if "web" in normalized["taint"] and normalized["capability"] != "none":
        raise SecurityViolation("web-tainted state cannot be auto-run")


def require_sandbox_for_bang(*, sandbox_exists: bool) -> None:
    """Reserve future `!` execution for an explicit sandbox boundary."""
    if not sandbox_exists:
        raise SecurityViolation("bang execution requires a sandbox")


def inherited_label(metadata: dict[str, Any]) -> str:
    """Return a compact label for inherited trust shown in terminal status."""
    normalized = normalize_trust_record(metadata)
    taint = normalized["taint"]
    if "legacy" in taint:
        return "legacy"
    if "web" in taint:
        return "web"
    if "model" in taint:
        return "model"
    if normalized["integrity"] == "local_model":
        return "model"
    return normalized["integrity"]


def candidate_prefix(metadata: dict[str, Any]) -> str:
    """Return the trust prefix shown beside selectable command candidates."""
    normalized = normalize_trust_record(metadata)
    if "legacy" in normalized["taint"]:
        return "[legacy/low-trust]"
    if "web" in normalized["taint"]:
        if normalized["provisional"]:
            return "[web-tainted/provisional]"
        return "[web-tainted/read]"
    if normalized["integrity"] == "local_model":
        return f"[model/{normalized['capability']}]"
    return f"[{normalized['integrity']}/{normalized['capability']}]"
