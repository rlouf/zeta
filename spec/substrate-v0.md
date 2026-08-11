# Zeta substrate, version 0 (`substrate-v0`)

Status: normative.

This specification defines the identity model shared by the Python substrate
and the Rust `zeta-substrate` package (`zeta_substrate` in Rust code). It
defines immutable values, canonical JSON, BLAKE3 addresses, and mutable refs.
It does not define a journal schema or a durable substrate-store format.

The key words MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY are interpreted as
described in RFC 2119.

## 1. Model

An `Object` is an immutable value in a Merkle DAG. It has four identity-bearing
fields:

| field | type | meaning |
|---|---|---|
| `kind` | string | the object's domain role |
| `schema` | string | the schema for `data` |
| `data` | object | the schema-defined payload |
| `links` | array of object ids | ordered structural edges to other objects |

The order of `links` is significant. A link is an address string. A store MAY
accept a link before it holds the linked object.

A `Derivation` records provenance. It has four identity-bearing fields:

| field | type | meaning |
|---|---|---|
| `producer` | string | the operation that produced the output |
| `output_id` | object id | the produced object |
| `input_ids` | array of object ids | the ordered inputs |
| `params` | object | stable producer parameters |

A `Ref` gives a stable name to one immutable object. It has `name` and
`object_id` fields. A store moves a ref conditionally: the caller supplies the
expected current object id and the requested new object id. `RefUpdate` reports
the name, the object id observed before the move, the requested new object id,
and whether the move succeeded. A failed conditional move does not change the
ref.

## 2. Canonical JSON

Every identity-bearing JSON value has one canonical UTF-8 representation:

- Object keys are strings and are sorted in Unicode code-point order.
- Arrays keep their input order. Python tuples are encoded as JSON arrays.
- Separators are `,` and `:` with no surrounding whitespace.
- Non-ASCII characters are emitted as UTF-8, not `\uXXXX` escapes.
- JSON-required escapes are used for quotation marks, reverse solidus, and
  control characters.
- The output has no byte-order mark and no trailing newline.
- `null`, booleans, strings, arrays, and objects use their JSON forms.

Python defines the reference encoding:

```python
json.dumps(
    value,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
    allow_nan=False,
).encode("utf-8")
```

### 2.1 Integers

An identity-bearing integer MUST fit either `i64` or `u64`. The accepted range
is `-9223372036854775808` through `18446744073709551615`. A mint MUST reject an
integer outside that range before it computes an address.

### 2.2 Floats

An identity-bearing float MUST be a finite IEEE 754 binary64 value. A mint MUST
reject NaN, positive infinity, negative infinity, and values that cannot be
represented as binary64.

The spelling matches Python's shortest round-trip representation:

- An integral float keeps `.0`, as in `1.0`.
- Negative zero is `-0.0`.
- Fixed notation is used when the normalized decimal exponent is from `-4`
  through `15`. Scientific notation is used outside that range.
- Scientific notation uses a lowercase `e`, always includes `+` or `-`, and
  uses at least two exponent digits. Examples are `1e+30`, `1e-05`, and
  `1e-06`.

The encoding vectors pin the boundary and precision cases used by the system.
Both implementations MUST reproduce their `canonical_utf8` values byte for
byte.

## 3. Addresses

Every address is `b3:` followed by exactly 64 lowercase hexadecimal
characters. The digest is the full 32-byte BLAKE3 output. An implementation
MUST NOT accept uppercase, truncation, or a different prefix as an address.

Exact content bytes use plain, undomained BLAKE3:

```text
content_address(bytes) = "b3:" + lowercase_hex(BLAKE3(bytes))
```

Domainless content hashing makes equal bytes share one address in every tool.
Files, payload bytes, and blob-store entries use this operation.

Structured identities use BLAKE3 derive-key mode:

```text
derived_address(context, bytes) =
    "b3:" + lowercase_hex(BLAKE3_derive_key(context, bytes))
```

The active contexts are frozen verbatim:

| identity | context |
|---|---|
| object | `zeta-os 2026-08 cas object` |
| derivation | `zeta-os 2026-08 cas derivation` |
| journal entry or deterministic chain link | `zeta-os 2026-08 cas chain` |

Durable runtime ids such as `evt_`, `qi_`, `att_`, `run_`, `pub_`, and
`wait_` are semantic identifiers, not content addresses.

The chain domain identifies journal entries and deterministic runtime links
such as publish and wait handles. `spec/journal-v0.md` §4 defines journal entry
bytes.

This version defines one address epoch. It defines no compatibility parser,
fallback mint, dual-read rule, or data migration. Data from another epoch MUST
be regenerated before use with this version.

## 4. Object and derivation identity

The canonical object value is:

```text
{"data": data, "kind": kind, "links": links, "schema": schema}
```

Spaces above are illustrative. The bytes use §2. An object's address is the
object-domain derived address of those bytes.

The canonical derivation value is:

```text
{"input_ids": input_ids, "output_id": output_id,
 "params": params, "producer": producer}
```

Its address is the derivation-domain derived address of those bytes.

`content_address()` is the only Object and Derivation mint exposed by the
Python and Rust substrate APIs.

## 5. Storage boundary

The Python substrate stores Objects, Derivations, and Refs. It resolves only
the address syntax in §3. Prefix resolution applies to the 64-character BLAKE3
digest.

The Rust `zeta-substrate` package contains the value types and a content
`BlobStore`. The blob store uses the `Fanout2` layout under its configured
root. The package does not yet persist Objects, Derivations, or Refs.
Backend-neutral journal values, chaining, verification, and in-memory
conformance live in the sibling `zeta-journal` package; neither package owns a
database or durable journal adapter.

The `zeta-ipc` crate carries addresses as protocol data. It validates address
syntax locally and does not depend on `zeta-substrate` or a hashing library.

## 6. Conformance vectors

The normative vectors are:

- `spec/vectors/substrate/encoding.json` for canonical JSON bytes;
- `spec/vectors/substrate/b3-addresses.json` for Object and Derivation values;
- `spec/vectors/addresses/vectors.json` for all active domains and plain
  content hashing.

Python generated the encoding vectors before the substrate mint changed. It
generated the active address vectors after the BLAKE3-only mint landed. Python
and Rust MUST read the same files in their tests.
