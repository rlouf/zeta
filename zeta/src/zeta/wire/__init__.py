"""wire-v0 protocol implementation (spec/wire-v0.md).

`envelopes` and `framing` implement the encoding both sides share.
`plugin` is the child-side SDK; it stays importable without the rest
of Zeta so it can later extract into a standalone package. `host` is
the runtime-side supervisor.
"""

from zeta.wire.envelopes import (
    PROTOCOL_VERSION,
    EnvelopeError,
    canonical_json,
    envelope,
    mint_event_id,
    validate_envelope,
)
from zeta.wire.host import SourceCommand, SubprocessSource, WireEvent
from zeta.wire.plugin import EventType, SourceEvent, run_source

__all__ = [
    "EnvelopeError",
    "EventType",
    "PROTOCOL_VERSION",
    "SourceCommand",
    "SourceEvent",
    "SubprocessSource",
    "WireEvent",
    "canonical_json",
    "envelope",
    "mint_event_id",
    "run_source",
    "validate_envelope",
]
