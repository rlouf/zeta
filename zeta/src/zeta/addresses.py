"""Content addresses for durable identifiers.

Every identifier minted from content goes through this module, so the
address format has one owner. Addresses use BLAKE3 in derive-key mode
with one frozen context string per domain. The context strings are
opaque and stay verbatim forever: a product rename never changes them,
because changing one would silently re-address every stored record in
its domain.

Legacy identifiers minted from SHA-256 (`sha256:`-prefixed, or bare
24- or 64-hex digests) remain valid forever. `is_legacy` names them so
call sites that compare or look identifiers up can dual-read instead
of re-hashing history.

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
BLOB_CONTEXT = "zeta-os 2026-08 cas blob"
CHAIN_CONTEXT = "zeta-os 2026-08 cas chain"
PROMPT_CONTEXT = "zeta-os 2026-08 cas prompt"
SKILL_CONTEXT = "zeta-os 2026-08 cas skill"

CONTEXTS = {
    "event": EVENT_CONTEXT,
    "blob": BLOB_CONTEXT,
    "chain": CHAIN_CONTEXT,
    "prompt": PROMPT_CONTEXT,
    "skill": SKILL_CONTEXT,
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


def blob_address(data: bytes) -> str:
    """Return the address that identifies one payload's exact bytes."""
    return address("blob", data)


def chain_address(data: bytes) -> str:
    """Return the address for one derived link in the idempotent id chain."""
    return address("chain", data)


def prompt_address(data: bytes) -> str:
    """Return the address for one record in the prompt-trace substrate."""
    return address("prompt", data)


def skill_address(data: bytes) -> str:
    """Return the address that identifies one skill body."""
    return address("skill", data)


def is_b3(identifier: str) -> bool:
    """Return whether `identifier` is a well-formed full-width `b3:` address."""
    if not identifier.startswith(B3_PREFIX):
        return False
    digest = identifier[len(B3_PREFIX) :]
    return len(digest) == 64 and _is_hex(digest)


def is_legacy(identifier: str) -> bool:
    """Return whether `identifier` belongs to the SHA-256 epoch.

    Legacy shapes stay valid forever: a `sha256:`-prefixed content
    address, or a bare digest of exactly 24 hex characters (the old
    truncated handles) or 64 hex characters (the old full digests).
    """
    if identifier.startswith("sha256:"):
        return True
    if ":" in identifier:
        return False
    return len(identifier) in (24, 64) and _is_hex(identifier)


def _is_hex(value: str) -> bool:
    return bool(value) and all(character in _HEX_DIGITS for character in value)
