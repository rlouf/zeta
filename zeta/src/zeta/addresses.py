"""BLAKE3 content addresses and domain-separated identifiers.

Plain content bytes share one undomained hash universe. Structured
identities use frozen derive-key contexts so values from different
domains cannot collide semantically.

The wire protocol (`spec/wire-v0.md` §11) and the conformance vectors
in `spec/vectors/addresses/vectors.json` pin this module's outputs
byte-for-byte for other implementations.

This module derives values and holds no state. It imports nothing from
Zeta, so any layer may use it.
"""

from __future__ import annotations

import string

from blake3 import blake3

B3_PREFIX = "b3:"

EVENT_CONTEXT = "zeta-os 2026-08 cas event"
CHAIN_CONTEXT = "zeta-os 2026-08 cas chain"
OBJECT_CONTEXT = "zeta-os 2026-08 cas object"
DERIVATION_CONTEXT = "zeta-os 2026-08 cas derivation"

CONTEXTS = {
    "event": EVENT_CONTEXT,
    "chain": CHAIN_CONTEXT,
    "object": OBJECT_CONTEXT,
    "derivation": DERIVATION_CONTEXT,
}

_HEX_DIGITS = frozenset(string.hexdigits.lower())


def address(domain: str, data: bytes) -> str:
    """Return the `b3:` address of `data` in one of the frozen domains."""
    context = CONTEXTS.get(domain)
    if context is None:
        raise ValueError(f"unknown address domain {domain!r}")
    return B3_PREFIX + blake3(data, derive_key_context=context).hexdigest()


def event_address(data: bytes) -> str:
    """Return the address that identifies one wire event envelope."""
    return address("event", data)


def content_address(data: bytes) -> str:
    """Return the plain-BLAKE3 address of exact bytes.

    Content hashing is deliberately domainless: a file's bytes, a
    pack blob, and an event's `payload_hash` are the same string for
    the same bytes, in every implementation.
    """
    return B3_PREFIX + blake3(data).hexdigest()


def chain_address(data: bytes) -> str:
    """Return the address for one derived link in the idempotent id chain."""
    return address("chain", data)


def object_address(data: bytes) -> str:
    """Return the address for one immutable substrate object."""
    return address("object", data)


def derivation_address(data: bytes) -> str:
    """Return the address for one substrate provenance edge."""
    return address("derivation", data)


def is_b3(identifier: str) -> bool:
    """Return whether `identifier` is a well-formed full-width `b3:` address."""
    if not identifier.startswith(B3_PREFIX):
        return False
    digest = identifier[len(B3_PREFIX) :]
    return len(digest) == 64 and _is_hex(digest)


def _is_hex(value: str) -> bool:
    return bool(value) and all(character in _HEX_DIGITS for character in value)
