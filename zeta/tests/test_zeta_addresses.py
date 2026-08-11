"""Content-address minting and legacy dual-read tests."""

import base64
import json
from pathlib import Path

import pytest

from zeta import addresses

VECTORS_PATH = (
    Path(__file__).resolve().parents[2]
    / "spec"
    / "vectors"
    / "addresses"
    / "vectors.json"
)


def load_vectors() -> dict:
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


def vector_input_bytes(vector: dict) -> bytes:
    if "input_utf8" in vector:
        return vector["input_utf8"].encode("utf-8")
    return base64.b64decode(vector["input_base64"])


def minted(vector: dict) -> str:
    data = vector_input_bytes(vector)
    if vector["domain"] == "content":
        return addresses.content_address(data)
    return addresses.address(vector["domain"], data)


def test_address_vectors_match_byte_for_byte() -> None:
    document = load_vectors()
    assert document["contexts"] == addresses.CONTEXTS
    assert document["vectors"], "vector file must not be empty"
    for vector in document["vectors"]:
        assert minted(vector) == vector["address"], vector["name"]


def test_address_vectors_cover_every_domain_and_content() -> None:
    document = load_vectors()
    assert {vector["domain"] for vector in document["vectors"]} == set(
        addresses.CONTEXTS
    ) | {"content"}


def test_content_address_is_plain_undomained_blake3() -> None:
    from blake3 import blake3

    data = b"the exact payload bytes"
    assert addresses.content_address(data) == "b3:" + blake3(data).hexdigest()
    minted_addresses = {
        addresses.content_address(data),
        *(addresses.address(domain, data) for domain in addresses.CONTEXTS),
    }
    assert len(minted_addresses) == len(addresses.CONTEXTS) + 1


def test_retired_blob_context_stays_reserved() -> None:
    assert addresses.RETIRED_BLOB_CONTEXT == "zeta-os 2026-08 cas blob"
    assert "blob" not in addresses.CONTEXTS


def test_address_is_full_width_lowercase_hex() -> None:
    value = addresses.event_address(b"payload")
    assert value.startswith("b3:")
    digest = value.removeprefix("b3:")
    assert len(digest) == 64
    assert digest == digest.lower()
    assert all(character in "0123456789abcdef" for character in digest)


def test_address_is_deterministic() -> None:
    assert addresses.chain_address(b"qi_evt_1:0") == addresses.chain_address(
        b"qi_evt_1:0"
    )


def test_address_domains_are_separated() -> None:
    data = b"same input"
    minted = {addresses.address(domain, data) for domain in addresses.CONTEXTS}
    assert len(minted) == len(addresses.CONTEXTS)


def test_address_rejects_unknown_domain() -> None:
    with pytest.raises(ValueError):
        addresses.address("journal", b"data")


def test_is_b3_accepts_minted_addresses() -> None:
    assert addresses.is_b3(addresses.skill_address(b"body"))


def test_is_b3_rejects_legacy_and_malformed_values() -> None:
    assert not addresses.is_b3("sha256:" + "a" * 64)
    assert not addresses.is_b3("b3:" + "a" * 24)
    assert not addresses.is_b3("b3:" + "G" * 64)
    assert not addresses.is_b3("a" * 64)


def test_is_legacy_accepts_sha256_epoch_values() -> None:
    assert addresses.is_legacy("sha256:" + "a" * 64)
    assert addresses.is_legacy("a" * 24)
    assert addresses.is_legacy("f" * 64)


def test_is_legacy_rejects_b3_and_non_hash_values() -> None:
    assert not addresses.is_legacy(addresses.event_address(b"x"))
    assert not addresses.is_legacy("evt_" + "a" * 32)
    assert not addresses.is_legacy("a" * 32)
    assert not addresses.is_legacy("file:inbox/todo.txt")
