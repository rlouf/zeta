# Zeta journal, version 0 (`journal-v0`)

Status: normative.

This specification defines the logical journal shared by Python and the Rust
`zeta::journal` module. Its canonical JSON and BLAKE3 addressing come from the
sibling `zeta::substrate` module as defined by `spec/substrate-v0.md`. It does
not define a database schema, file layout, payload placement policy, migration
procedure, projection, or coordination state.

The key words MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY are interpreted as
described in RFC 2119.

## 1. Ownership boundary

The journal is an append-ordered sequence of immutable events. It defines the
event values, payload identity, append and query behavior, chain identity, and
verification rules that every backend implements.

Dispatch owns the storage backend, its connection or client, and transaction
boundaries. A backend MAY store canonical payload bytes inline, out of line,
compressed, or remotely. Such choices MUST NOT change the logical event,
payload address, chain bytes, or entry address.

## 2. Event model

A `DraftEvent` contains:

| field | type | meaning |
|---|---|---|
| `type` | non-empty string | event vocabulary; APIs MAY name it `event_type` |
| `source` | non-empty string | producer identity |
| `payload` | object | event-specific JSON value |
| `idempotency_key` | string or null | global retry identity |
| `caused_by` | event id or null | causal parent |
| `session_id` | string or null | session association |
| `run_id` | string or null | run association |
| `turn_id` | string or null | turn association |

A durable `Event` adds:

| field | type | meaning |
|---|---|---|
| `id` | non-empty string | opaque event identity |
| `timestamp_ms` | i64 | producer timestamp in Unix milliseconds |
| `cursor` | positive integer | backend-assigned append order |

Generated event ids conventionally use `evt_` followed by 32 lowercase UUID
hexadecimal digits. Directly appended ids remain opaque strings. An `evt_` id
is not a BLAKE3 address and is never parsed as one.

Cursors are strictly increasing across successful appends but need not be
contiguous. They order and paginate events; timestamps do not. A journal entry
pairs one Event with its exact canonical payload bytes, payload address,
previous entry address, and entry address. These identity values do not change
the caller-visible Event fields.

## 3. Payload identity

An event payload MUST be a JSON object. Its identity is:

```text
payload_bytes   = canonical_json(payload)
payload_address = content_address(payload_bytes)
```

`canonical_json` and domainless `content_address` are exactly
`spec/substrate-v0.md` §§2–3. Append MUST reject non-string object keys,
integers outside the i64/u64 union, non-finite floats, values outside finite
binary64, and non-JSON values.

The bytes are independent of physical placement. A read MUST return the same
logical payload object no matter how its backend keeps those bytes.

## 4. Chain identity

### 4.1 Canonical input

The version-0 chain value is this JSON array in this exact order:

```text
[
  0,
  id,
  type,
  source,
  payload_address,
  idempotency_key,
  caused_by,
  session_id,
  run_id,
  turn_id,
  timestamp_ms,
  previous_address
]
```

Every position is always present. Nullable fields and the genesis predecessor
are explicit JSON `null`. The canonical bytes use substrate-v0 §2. The leading
integer `0` is the chain format version.

Every semantic Event field participates. `cursor` is excluded because it is a
backend ordering token; the predecessor link already commits append order.
Payload bytes participate through `payload_address`, not by being embedded
again. A public entry mint nevertheless requires the Event's assigned positive
cursor because it produces a durable JournalEntry rather than a pre-append
chain preview.

### 4.2 Entry address and linkage

```text
entry_address = derived_address(
  "zeta-os 2026-08 cas chain",
  canonical_json(chain_value),
)
```

The first successful append has `previous_address = null`. Every later
successful append names the immediately preceding successful entry address.
A duplicate attempt creates no entry and leaves the head unchanged.
Mint APIs MUST reject payload or predecessor inputs that are not full-width
lowercase `b3:` addresses.

The Chain context also serves deterministic runtime chain links. The
version-framed JSON array distinguishes journal bytes from those raw inputs.

## 5. Append and idempotency

Append publishes one complete Event and chain entry into a single total order.
A conforming implementation MUST:

1. Require non-empty `id`, `type`, and `source` strings.
2. Look up the candidate `id`.
3. If no id matches and `idempotency_key` is non-null, look it up globally.
4. On a match, return the existing event with `inserted = false`. An id match
   takes precedence when the candidate id and key name different events. The
   candidate payload and remaining metadata are not compared. Step 1 still
   applies to every attempt before duplicate resolution.
5. For a new event, validate and canonicalize the payload, read the current
   head, assign a cursor greater than every prior cursor, and compute §3–4.
6. Publish the Event, identity values, and new head atomically, then return the
   new Event with `inserted = true`.

A failure publishes neither an event nor a new head. Concurrent successful
appends MUST form one total order. Concurrent candidates sharing an id or
non-null idempotency key insert at most one event. The storage transaction that
provides these guarantees belongs to Dispatch and is outside this specification.

## 6. Queries

`list_events` combines every supplied filter with logical AND:

- exact `type`;
- case-sensitive, literal `type_prefix`;
- exact `session_id`, `run_id`, `turn_id`, or `caused_by`;
- `cursor > after_cursor`.

Results are cursor-ascending by default and cursor-descending when
`newest_first` is true. A non-negative `limit` is applied after ordering;
zero returns no events. A negative limit MUST be rejected.

`get(id)` returns the exact event or none. `children(id)` is the
`caused_by = id` filter in cursor order. Turn and run conveniences are ordinary
filters.

`causal_chain(id)` follows `caused_by` and stops at null, a missing parent, or
a repeated id. It returns the oldest event reached through the requested event.
Missing parents and causal cycles are valid application metadata, not journal
chain corruption.

Journal-v0 defines no delete or clear operation. Retention is a storage policy
that must preserve whatever verification guarantee its owner claims; no
in-database seal can recreate deleted history.

## 7. Verification

Full verification walks entries in cursor order and MUST:

1. require strictly increasing cursors;
2. reject duplicate event ids and duplicate non-null idempotency keys;
3. canonicalize each logical payload and compare the exact bytes and
   `payload_address`;
4. require a null predecessor for genesis and the prior computed entry address
   thereafter;
5. reconstruct the complete §4.1 value and compare `entry_address`.

Verification is read-only and reports the first divergence with its stable
reason, event id when available, cursor when available, and number of entries
already checked. Stable reasons in v0 are `cursor_order`, `duplicate_id`,
`duplicate_idempotency_key`, `payload_encoding`, `payload_address`,
`previous_address`, `entry_address`, and `expected_head`.

Successful verification returns the number of checked entries and the current
head, which is null for an empty journal. Unanchored verification proves only
internal consistency of the retained sequence. A caller MAY supply an
externally trusted expected head; an explicitly supplied null value anchors an
empty journal, while omitting the argument requests unanchored verification.
Matching the anchor also detects deletion of the final suffix.
“Tamper-evident” applies only relative to such an external anchor.

## 8. Storage exclusions

Journal-v0 defines no database engine, schema, indexes, connection lifecycle,
transaction API, file layout, payload-size threshold, blob store, compression,
garbage collection, migration, backfill, compatibility epoch, seal, or repair
operation. An implementation's storage adapter is conforming when it presents
the exact logical behavior and identities above.

The committed pre-Phase-2 SQLite fixture is a regression oracle for the Python
runtime, not a journal-v0 storage artifact and not a format to migrate.

## 9. Conformance vectors

The normative vectors are:

- `spec/vectors/journal/chain.json` for complete events, exact canonical
  payload and chain bytes, payload addresses, predecessors, and entry
  addresses;
- `spec/vectors/journal/operations.json` for inserts, id-first duplicate
  resolution, idempotency, cursor/query ordering, literal prefixes, and causal
  traversal.

Python generated both files from the pre-change journal behavior plus the
already-frozen substrate primitives. Python and Rust MUST consume the same
files directly and reproduce every byte, address, append result, query result,
causal result, and final head.
