# Zeta wire protocol, version 0 (`wire-v0`)

Status: normative.
Audience: anyone implementing a Zeta plugin or a Zeta runtime in any
language. This document stands alone: no access to the Zeta source is
required or assumed.

The key words MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY are to be
interpreted as described in RFC 2119.

Conformance vectors for this specification live in `spec/vectors/`.
An implementation MUST pass them; see `spec/vectors/README.md`.

## 1. Model

A **runtime** (the parent) supervises a **plugin** (a child process).
Version 0 defines one plugin role in full operation, `source`: a
plugin that produces events for the runtime's durable journal. The
`role` field is fully defined (§5.2) so later versions can activate
the other roles without an envelope change.

- The parent spawns the child and owns its lifetime.
- The child writes protocol messages to **stdout** and nothing else.
- The parent writes protocol messages to the child's **stdin**.
- The child writes logs and diagnostics to **stderr** only. Any
  non-protocol byte on stdout is a protocol error (§10).

## 2. Transport and framing

Version 0 frames messages as **newline-delimited JSON**:

- One JSON object per line ("envelope"), encoded as UTF-8, terminated
  by a single `\n` (0x0A). A message MUST NOT contain a raw newline
  byte inside the JSON text; newlines inside JSON strings use the
  two-character escape `\n` as usual.
- There are no length prefixes in version 0. A length-prefixed binary
  framing is **reserved** for a future protocol version and MUST NOT
  be emitted by v0 implementations.
- An empty line is a framing violation. A line that is not a single
  well-formed JSON object is a framing violation. A receiver MUST NOT
  crash on a framing violation: it MUST log the line (or a bounded
  prefix of it), and it MAY send an `error` envelope with code
  `protocol` and/or terminate the peer (parents terminate children;
  children exit). Partial lines at end-of-stream are discarded.

### 2.1 Canonical serialization

Writers SHOULD emit the canonical form; readers MUST accept any valid
JSON serialization of a valid envelope. The canonical form is:

- object keys sorted lexicographically (byte order of the UTF-8 key),
- no insignificant whitespace (`,` and `:` as the only separators),
- non-ASCII characters emitted literally as UTF-8 (no `\uXXXX`
  escaping of characters above U+007F),
- no floating-point representations of integral numbers (`0`, not
  `0.0`),
- optional fields that are absent are omitted; fields whose value is
  `null` and that are defined as nullable are emitted as `null`.

The conformance vectors state each envelope in canonical form: parsing
a valid vector and re-serializing it canonically MUST reproduce the
file's bytes minus the trailing newline.

## 3. Envelope

Every message is one envelope. Common fields:

| field  | type   | required | meaning |
|--------|--------|----------|---------|
| `v`    | int    | yes      | protocol version of this envelope; `0` in v0 |
| `kind` | string | yes      | message kind, §4 |
| `id`   | string | yes      | message id, unique per sender within the session |
| `ts`   | string | yes      | RFC 3339 UTC timestamp with `Z` suffix |

Rules:

- `v` MUST be a non-negative integer. In v0 every envelope carries
  `v: 0` once the handshake has selected version 0.
- `id` MUST be a non-empty string. For `kind: "event"` see §6.1.
- `ts` MUST be an RFC 3339 date-time in UTC using the `Z` designator
  (e.g. `2026-08-10T12:00:00Z`; fractional seconds permitted).
  Timestamps carrying a numeric offset (`+02:00`) are invalid in v0.
- **Unknown envelope fields MUST be ignored.** This is the v0 forward
  compatibility rule.
- An unknown `kind` is an error: the receiver responds with an
  `error` envelope, code `protocol`, and MAY terminate the peer.

## 4. Kinds

Version 0 defines exactly these kinds:

| kind        | direction        | purpose |
|-------------|------------------|---------|
| `hello`     | child → parent   | handshake open, §5 |
| `hello_ack` | parent → child   | handshake close, §5 |
| `event`     | child → parent   | one event, §6 |
| `ack`       | parent → child   | acknowledge one event, §7 |
| `heartbeat` | child → parent   | liveness, §8 |
| `error`     | either direction | error report, §10 |
| `shutdown`  | parent → child   | orderly stop, §9 |

**Reserved kinds.** The kind names `event_batch`, `call`, and
`call_result` are reserved for future versions. A v0 implementation
MUST NOT emit them, and MUST treat receiving one exactly like an
unknown kind (protocol error). Their intended future meanings, for
context only: `event_batch` carries several events in one envelope
under the length-prefixed framing; `call` / `call_result` carry
request/response invocations for the `tool` and `provider` roles.

## 5. Handshake

The child speaks first. Any traffic before a completed handshake —
any child envelope other than `hello` first, or any parent envelope
before it has received `hello` — is a protocol error.

### 5.1 `hello` (child → parent)

Kind-specific fields:

| field               | type          | required | meaning |
|---------------------|---------------|----------|---------|
| `name`              | string        | yes      | plugin name, e.g. `fs-inbox` |
| `plugin_version`    | string        | yes      | plugin's own version string |
| `role`              | string        | yes      | `source` \| `tool` \| `provider`, §5.2 |
| `protocol_versions` | array of int  | yes      | protocol versions the plugin speaks, non-empty |
| `event_types`       | array         | for `source` | event types it may emit, §5.3 |
| `capabilities`      | object        | no       | capability flags, §5.4 |
| `heartbeat_secs`    | number        | no       | heartbeat interval, default `10`, range [1, 300] |
| `ack_window`        | int           | no       | max unacknowledged events, default `64`, range [1, 1024] |

### 5.2 Roles

- `source`: the plugin produces events (`event` envelopes) and the
  parent acknowledges them. **Only `source` is implemented in v0.**
- `tool`: reserved. The plugin will execute `call` requests from the
  parent and answer with `call_result`. A v0 parent MUST reject a
  `tool` hello with `error` code `unsupported_version`-class handling
  (see §10; the code is `protocol` since the version negotiated fine
  but the role is unavailable — parents SHOULD use code `protocol`
  with a message naming the role) and terminate the child.
- `provider`: reserved. The plugin will serve model or capability
  invocations initiated by the parent, also over `call` /
  `call_result`. Same v0 rejection rule as `tool`.

### 5.3 `event_types`

Each entry is an object `{"type": <string>, "schema": <string>}`.

- `type` is the event type string as it appears on `event` envelopes
  (e.g. `file.created`).
- `schema` is a schema reference of the form `name@version` where
  `version` is a positive integer major version (`file.created@1`).
  In v0 a schema reference is a **name**, resolved by the runtime
  against its own schema registry; the runtime's registry is
  authoritative and the parent validates event payloads against it.
  **In a later version schema references become content addresses**
  (`b3:` addresses of the schema document, §11); implementations MUST
  treat the field as an opaque string and MUST NOT parse meaning out
  of it beyond the `name@version` split.

### 5.4 `capabilities`

An object of named boolean or scalar flags. Unknown capabilities MUST
be ignored. Version 0 defines and reserves:

- `effects_are_proposals` (bool, default `false`): declares that the
  plugin's effects are **staged proposals requiring a gate**, not
  immediate actions. Intended future meaning: when `true`, everything
  this plugin does on the outside world is first recorded as a
  proposal event that a policy gate (human or automated) must accept
  before the runtime treats the effect as performed. In v0 the flag
  is carried and recorded but has no behavioral effect.

### 5.5 `hello_ack` (parent → child)

| field              | type   | required | meaning |
|--------------------|--------|----------|---------|
| `protocol_version` | int    | yes      | the version chosen by the parent |
| `runtime`          | string | yes      | runtime identification, e.g. `zeta-os/0.10.2` |

The parent chooses the highest protocol version present in both its
own supported set and the child's `protocol_versions`. If the
intersection is empty the parent sends `error` with code
`unsupported_version` and terminates the child. After `hello_ack`,
both sides emit envelopes with `v` equal to the chosen version; an
envelope with a different `v` after the handshake is a protocol
error.

The parent MUST apply a handshake timeout: a child that has not
delivered a valid `hello` within the timeout (implementation-chosen;
the Zeta runtime uses 10 s) is killed.

## 6. Events

An `event` envelope carries kind-specific fields:

| field        | type           | required | meaning |
|--------------|----------------|----------|---------|
| `type`       | string         | yes      | event type string |
| `schema`     | string         | yes      | schema reference, §5.3 |
| `caused_by`  | string or null | yes      | id of the causing event, or `null` |
| `session_id` | string or null | yes      | durable session scope, or `null` |
| `payload`    | object         | see below | inline payload |
| `payload_hash` | string       | see below | content address of the payload, §11 |

- An event MUST carry **exactly one** of `payload` and
  `payload_hash`. Carrying both, or neither, is invalid.
- `payload` is permitted only when its serialized size — the UTF-8
  byte length of its canonical JSON serialization (§2.1) — is at most
  **65536 bytes (64 KiB)**. Larger payloads MUST be referenced by
  `payload_hash`.
- `payload_hash` is a `b3:` address in the **blob** domain (§11).
  Version 0 defines no blob transfer mechanism: a receiver that
  cannot resolve the referenced content rejects the event with
  `error` code `unsupported` (retryable `false`). The Zeta Phase-0
  runtime stores payloads inline and always rejects `payload_hash`
  events this way.

### 6.1 Event ids

The `id` of an `event` envelope identifies the event for
acknowledgment. It MUST be unique among the child's unacknowledged
events. It SHOULD be the `b3:` address, in the **event** domain
(§11), of the event's identity bytes: the canonical JSON serialization
(§2.1) of the object `{"payload": <payload>, "type": <type>}`. A
deterministic source then mints the same id for the same event on
every run, which is what makes redelivery after a restart idempotent
for downstream consumers.

## 7. Acknowledgment and flow control

- The parent sends `ack` for each accepted `event`. Kind-specific
  field: `event_id` (string, required) — the `id` of the event being
  acknowledged. Acceptance means the parent has durably recorded the
  event (or recognized it as a duplicate of one it already recorded).
- A child MUST NOT have more than `ack_window` (from its `hello`;
  default 64) unacknowledged events outstanding. Exceeding the window
  is a **protocol error on the child**: the parent MAY kill it. A
  compliant child stops emitting and waits for acks when the window
  is full.
- A parent that cannot keep up simply withholds acks. Withheld acks
  are backpressure, not an error.
- An `ack` whose `event_id` matches nothing outstanding MUST be
  ignored by the child (it can happen after a child-side retry).

## 8. Liveness

- The child emits `heartbeat` every `heartbeat_secs` seconds (from
  its `hello`; default 10). A `heartbeat` has no kind-specific
  fields. Any valid envelope from the child also counts as a sign of
  life; heartbeats keep the channel alive when there are no events.
- The parent treats **3 consecutive missed heartbeats** — no valid
  child envelope for more than 3 × `heartbeat_secs` — as child death,
  and MAY kill and respawn the child.
- Parents SHOULD respawn dead or killed children with exponential
  backoff, capped (the Zeta runtime doubles from 0.5 s, capped at
  30 s, and resets after a healthy period).

## 9. Shutdown

Orderly stop escalates:

1. The parent sends `shutdown`. Optional kind-specific field:
   `reason` (string). After `shutdown` the parent stops acking and
   the child SHOULD flush nothing further: it stops emitting events,
   and exits with status 0.
2. If the child has not exited after a **grace period** (default 5 s,
   implementation-configurable), the parent sends SIGTERM.
3. If the child still has not exited after a further grace period,
   the parent sends SIGKILL.

A child MAY exit on its own at any time; a source that is done
producing simply exits 0. The parent treats an unexpected exit as
death (§8) and applies its respawn policy.

## 10. Errors

An `error` envelope carries kind-specific fields:

| field       | type   | required | meaning |
|-------------|--------|----------|---------|
| `code`      | string | yes      | one of the enumerated codes below |
| `message`   | string | yes      | human-readable description |
| `retryable` | bool   | yes      | whether retrying the same input can succeed |

Enumerated codes (v0):

| code                  | meaning |
|-----------------------|---------|
| `protocol`            | framing violation, handshake violation, unknown or reserved kind, ack-window overflow, stdout pollution, wrong `v` after handshake |
| `schema`              | an event failed payload-schema validation, or an envelope failed shape validation |
| `internal`            | the sender hit an internal failure unrelated to the peer's input |
| `unsupported_version` | protocol version negotiation failed (§5.5) |
| `unsupported`         | the envelope is valid but uses a feature this implementation does not support (e.g. `payload_hash` in Phase 0) |

Receivers MUST accept unknown codes (treat as `internal`) so the set
can grow. An `error` envelope is informational; it does not itself
terminate the session. Termination is the supervisor's decision.

## 11. Content addresses

Zeta identifiers minted from content use BLAKE3 in **derive-key
mode**. For a domain with context string `C` and input bytes `B`:

```
address = "b3:" + lowercase_hex( BLAKE3_derive_key(context = C, key_material = B) )
```

where `BLAKE3_derive_key` is the standard BLAKE3 `derive_key` mode
producing the default 32-byte output. The string form is always `b3:`
followed by exactly 64 lowercase hex characters. **No truncation is
permitted.**

The domain context strings are frozen forever, verbatim. They are
opaque byte strings; a product rename never changes them:

| domain | context string |
|--------|----------------|
| event  | `zeta-os 2026-08 cas event` |
| blob   | `zeta-os 2026-08 cas blob` |
| chain  | `zeta-os 2026-08 cas chain` |
| prompt | `zeta-os 2026-08 cas prompt` |
| skill  | `zeta-os 2026-08 cas skill` |

Domains in wire-v0: **event** addresses identify `event` envelopes
(§6.1); **blob** addresses identify payload bytes (`payload_hash`,
§6). The **chain**, **prompt**, and **skill** domains identify
runtime-internal derived records and do not appear on the wire in v0;
they share the same address format and vectors.

### 11.1 Legacy identifiers

Before `b3:` addresses, Zeta minted identifiers from SHA-256. Those
identifiers remain valid forever; implementations that look
identifiers up or compare them MUST dual-read:

- a value prefixed `sha256:` is a legacy SHA-256 content address;
- a value that contains no `:` and consists of exactly 24 or exactly
  64 lowercase hex characters is a legacy bare (possibly truncated)
  SHA-256 digest.

New identifiers MUST be minted as full-width `b3:` addresses.

## 12. Versioning summary

- Unknown envelope fields: ignore (§3).
- Unknown `kind`: `error` code `protocol` (§3, §4).
- Version negotiation happens once, at handshake (§5.5); an empty
  intersection fails with `unsupported_version`.
- After the handshake, an envelope whose `v` differs from the chosen
  version is a protocol error.

## Appendix A — example session (informative)

Child → parent lines marked `C:`, parent → child `P:`. The scripted
version of this session, with direction markers, is the conformance
vector `spec/vectors/handshake/session-01.jsonl`.

```
C: {"v":0,"kind":"hello","id":"m-c-1","ts":"2026-08-10T12:00:00Z","name":"fs-inbox","plugin_version":"0.1.0","role":"source","protocol_versions":[0],"event_types":[{"type":"file.created","schema":"file.created@1"}],"capabilities":{"effects_are_proposals":false},"heartbeat_secs":10,"ack_window":64}
P: {"v":0,"kind":"hello_ack","id":"m-p-1","ts":"2026-08-10T12:00:00Z","protocol_version":0,"runtime":"zeta-os/0.10.2"}
C: {"v":0,"kind":"event","id":"b3:...64hex...","ts":"2026-08-10T12:00:01Z","type":"file.created","schema":"file.created@1","caused_by":null,"session_id":null,"payload":{"dir":"/p/inbox","name":"todo.txt","path":"/p/inbox/todo.txt"}}
P: {"v":0,"kind":"ack","id":"m-p-2","ts":"2026-08-10T12:00:01Z","event_id":"b3:...64hex..."}
C: {"v":0,"kind":"heartbeat","id":"m-c-3","ts":"2026-08-10T12:00:10Z"}
P: {"v":0,"kind":"shutdown","id":"m-p-3","ts":"2026-08-10T12:00:12Z","reason":"runtime stopping"}
```
