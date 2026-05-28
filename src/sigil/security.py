"""Small alpha trust records for Sigil events."""

from __future__ import annotations

from typing import Any, Literal, Sequence, cast

TrustMode = Literal["read-only", "propose", "execute-write"]
RiskLabel = Literal["network", "delete", "publish", "privileged"]

TRUST_MODES = frozenset({"read-only", "propose", "execute-write"})
RISK_LABELS = frozenset({"network", "delete", "publish", "privileged"})


def normalize_mode(value: object) -> TrustMode:
    """Return one of the alpha trust modes."""
    if isinstance(value, str) and value in TRUST_MODES:
        return cast(TrustMode, value)
    return "propose"


def normalize_labels(value: object) -> list[RiskLabel]:
    """Keep only user-facing risk labels."""
    if not isinstance(value, list):
        return []
    labels = []
    for item in value:
        if isinstance(item, str) and item in RISK_LABELS:
            labels.append(cast(RiskLabel, item))
    return sorted(set(labels), key=("network", "delete", "publish", "privileged").index)


def normalize_inputs(value: object) -> list[str]:
    """Normalize simple event references used by audit views."""
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item]


def normalize_trust_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return a record with only alpha trust fields."""
    normalized = dict(record)
    for field in ("capability", "integrity", "taint", "provisional"):
        normalized.pop(field, None)

    normalized["mode"] = normalize_mode(normalized.get("mode"))
    normalized["labels"] = normalize_labels(normalized.get("labels"))
    normalized["inputs"] = normalize_inputs(normalized.get("inputs"))
    return normalized


def create_trust_metadata(
    *,
    glyph: str,
    mode: TrustMode,
    labels: Sequence[str] = (),
    inputs: Sequence[str] = (),
    input_records: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Create alpha trust fields for a freshly produced value."""
    del input_records
    return {
        "glyph": glyph,
        "mode": mode,
        "labels": normalize_labels(list(labels)),
        "inputs": [item for item in inputs if item],
    }


def inherit_security(
    *,
    glyph: str,
    input_records: Sequence[dict[str, Any]],
    mode: TrustMode = "propose",
    labels: Sequence[str] = (),
) -> dict[str, Any]:
    """Create a simple audit record that points at prior event ids."""
    inherited_labels = set(labels)
    inputs = []
    for record in input_records:
        event_id = record_id(record)
        if event_id:
            inputs.append(event_id)
        inherited_labels.update(normalize_labels(record.get("labels")))
    return create_trust_metadata(
        glyph=glyph,
        mode=mode,
        labels=sorted(inherited_labels),
        inputs=inputs,
    )


def record_id(record: dict[str, Any]) -> str:
    """Return the event identifier used by audit links."""
    value = record.get("event_id") or record.get("id")
    return str(value) if value else ""


def inherited_label(metadata: dict[str, Any]) -> str:
    """Return the compact trust label shown in terminal status."""
    normalized = normalize_trust_record(metadata)
    labels = normalized["labels"]
    if labels:
        return ",".join(labels)
    return str(normalized["mode"])


def candidate_prefix(metadata: dict[str, Any]) -> str:
    """Return the trust prefix shown beside selectable command candidates."""
    normalized = normalize_trust_record(metadata)
    labels = normalized["labels"]
    suffix = f":{','.join(labels)}" if labels else ""
    return f"[{normalized['mode']}{suffix}]"
