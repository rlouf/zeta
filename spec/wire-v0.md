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
JSON serialization of a valid envelope. The canonical form is the
representation in `spec/substrate-v0.md` §2:

- object keys sorted in Unicode code-point order,
- no insignificant whitespace (`,` and `:` as the only separators),
- non-ASCII characters emitted literally as UTF-8 (no `\uXXXX`
  escaping of characters above U+007F),
- integers restricted to the union of `i64` and `u64`,
- finite binary64 floats in Python's shortest round-trip spelling,
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
- **A field whose value is JSON `null` is treated as absent**, both
  for required fields (the failure is `missing_field:<name>`, not a
  bad-value error) and for optional ones (readers skip them; the
  canonical form omits them). The only exceptions are the fields §6
  defines as required-but-nullable (`caused_by`, `session_id`),
  where `null` is a meaningful value.
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
| `call`      | parent → child   | invoke one declared operation, §7.1 |
| `call_result` | child → parent | answer one `call`, §7.1 |

**Reserved kinds.** The kind name `event_batch` is reserved for a
future version. A v0 implementation MUST NOT emit it, and MUST treat
receiving it exactly like an unknown kind (protocol error). Its
intended future meaning, for context only: several events in one
envelope under the length-prefixed framing.

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
| `event_types`       | array         | for `source` | event types it may emit, §5.3; may be empty |
| `operations`        | array         | no       | operations it serves via `call`, §5.4 |
| `capabilities`      | object        | no       | capability flags, §5.5 |
| `heartbeat_secs`    | number        | no       | heartbeat interval, default `10`, range [1, 300] |
| `ack_window`        | int           | no       | max unacknowledged events, default `64`, range [1, 1024] |

### 5.2 Roles

- `source`: the plugin produces events (`event` envelopes) and the
  parent acknowledges them. A source MAY additionally declare
  `operations` (§5.4) and serve `call`s; a source with an empty
  `event_types` array is a pure operations plugin (e.g. a
  notification connector with no ingress). **Only `source` is
  implemented in v0.**
- `tool`: reserved. The plugin executes `call` requests initiated by
  a running agent, with no journal side of its own. A v0 parent MUST
  reject a `tool` hello with an `error` of code `protocol` naming the
  role, and terminate the child.
- `provider`: reserved. The plugin serves model or capability
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

### 5.4 `operations`

Each entry is an object `{"name": <string>}`. `name` is the
operation the plugin will execute when the parent sends a `call`
envelope (§7.1) — for a connector, the egress event type it delivers
(e.g. `slack.message.post`). The authoritative metadata for an
operation (its payload schema, options schema, and delivery
semantics) lives in the plugin's describe manifest (§13); the
`hello` list states which of those operations this incarnation is
prepared to serve.

### 5.5 `capabilities`

An object of named boolean or scalar flags. Unknown capabilities MUST
be ignored. Version 0 defines and reserves:

- `effects_are_proposals` (bool, default `false`): declares that the
  plugin's effects are **staged proposals requiring a gate**, not
  immediate actions. Intended future meaning: when `true`, everything
  this plugin does on the outside world is first recorded as a
  proposal event that a policy gate (human or automated) must accept
  before the runtime treats the effect as performed. In v0 the flag
  is carried and recorded but has no behavioral effect.

### 5.6 `hello_ack` (parent → child)

| field              | type   | required | meaning |
|--------------------|--------|----------|---------|
| `protocol_version` | int    | yes      | the version chosen by the parent |
| `runtime`          | string | yes      | runtime identification, e.g. `zeta-os/0.10.2` |
| `config`           | object | no       | non-secret settings for this child, §5.7 |

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

### 5.7 Configuration and secrets

Twelve-factor at the boundary:

- **Non-secret settings** — watch directories, filters, poll
  intervals, feature switches — travel in `hello_ack.config`, an
  arbitrary JSON object the parent composes for this child. An
  absent `config` means `{}`. A child MUST NOT require settings from
  any other channel; argv and config files are the parent's private
  business, not part of this protocol.
- **Secrets** — tokens, signing keys — reach the child through its
  spawn-time **environment**, never through an envelope. Envelopes
  are journaled, logged, and replayed; the environment is not. A
  plugin that finds a required secret missing SHOULD report an
  `error` (code `internal`, `retryable: false`) naming the variable
  on stderr-visible terms (never echoing values) and exit non-zero.

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
- `payload_hash` is the **content address** of the payload bytes:
  the plain, undomained BLAKE3 of those exact bytes, `b3:`-prefixed
  (§11). Version 0 defines no blob
  transfer mechanism: a receiver that cannot resolve the referenced
  content rejects the event with `error` code `unsupported`
  (retryable `false`). The Zeta Phase-0 runtime stores payloads
  inline and always rejects `payload_hash` events this way.

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
- An ack is the durability boundary. A source holding an upstream
  cursor (a Telegram `getUpdates` offset, a Slack Socket Mode
  envelope, a queue lease) MUST confirm or advance that cursor only
  when the corresponding event is acked — never on emit. This closes
  the loss window between reading an upstream item and journaling
  it: an unacked item is redelivered after a restart, and the
  parent's idempotency keys absorb the duplicate.

### 7.1 Calls

A `call` invokes one operation the child declared in its `hello`
(§5.4). Kind-specific fields:

| field        | type   | required | meaning |
|--------------|--------|----------|---------|
| `name`       | string | yes      | the declared operation |
| `payload`    | object | yes      | operation input |
| `effect_key` | string | yes      | stable identity of the logical effect |

The child MUST answer every `call` with exactly one `call_result`:

| field       | type   | required | meaning |
|-------------|--------|----------|---------|
| `call_id`   | string | yes      | the `id` of the `call` envelope |
| `ok`        | bool   | yes      | whether the operation succeeded |
| `result`    | object | when `ok` | operation output |
| `error`     | object | when not `ok` | `{code, message, retryable}`, codes as §10 |

Rules:

- A `call` naming an undeclared operation is answered with
  `call_result` `ok: false`, error code `protocol`.
- `effect_key` identifies the logical effect across retries and
  respawns. The parent MAY re-issue a call with the same
  `effect_key` after a child death; a child whose provider supports
  deduplication SHOULD pass the key through (the declared delivery
  semantics in the describe manifest, §13, tell the parent what a
  retry may do).
- Calls and events multiplex freely on the same channel; a child
  may interleave `call_result`s with `event`s in any order. The
  parent correlates by `call_id`.
- The parent applies its own per-call timeout; a child death fails
  all outstanding calls (`internal`, retryable per the operation's
  declared semantics).

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

### 8.1 Crash-only reconnection

There is exactly one retry mechanism in this system, and it is the
supervisor's respawn. A plugin that holds a connection to the
outside world — a websocket, a long-poll, a subscription — MUST NOT
implement its own reconnect loop. When the connection is lost, the
child exits (non-zero); the supervisor's backoff-respawn IS the
reconnect logic, and the ack-gated cursors of §7 plus the parent's
idempotency keys make any redelivery harmless. One mechanism, one
backoff policy, one place to observe failures.

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
| `unsupported_version` | protocol version negotiation failed (§5.6) |
| `unsupported`         | the envelope is valid but uses a feature this implementation does not support (e.g. `payload_hash` in Phase 0) |

Receivers MUST accept unknown codes (treat as `internal`) so the set
can grow. An `error` envelope is informational; it does not itself
terminate the session. Termination is the supervisor's decision.

## 11. Addresses

`spec/substrate-v0.md` §3 defines the address syntax and hashing
operations for the whole system. Every address is `b3:` followed by
exactly 64 lowercase hexadecimal characters. No other address syntax
is valid in wire-v0.

An event `payload_hash` is the plain, undomained BLAKE3 address of
the canonical payload bytes. An event id SHOULD use the event-domain
context from substrate-v0 and the identity bytes in §6.1. The protocol
otherwise carries addresses as data. A protocol implementation MUST
NOT depend on a substrate hashing implementation to parse an envelope.

The active derived domains are event, chain, object, and derivation.
Only event addresses have wire-v0 behavior. Chain, object, and
derivation identities are runtime-internal and are defined by
substrate-v0.

## 12. Versioning summary

- Unknown envelope fields: ignore (§3).
- Unknown `kind`: `error` code `protocol` (§3, §4).
- Version negotiation happens once, at handshake (§5.6); an empty
  intersection fails with `unsupported_version`.
- After the handshake, an envelope whose `v` differs from the chosen
  version is a protocol error.

## 13. Connector executables

A **connector** is any executable that speaks wire-v0. Discovery is
the shell's:

- an executable named `zeta-connector-<id>` on `PATH` is the
  connector `<id>`;
- an executable file in a project's `agents/connectors/` directory is
  a project-local connector; its `<id>` is the file name (minus any
  extension).

There is no plugin registry, no entry points, and no enable list:
installing a package that provides `zeta-connector-slack` is what
registers the Slack connector, and a third-party connector in Go or
TypeScript joins by dropping an executable on `PATH`. A runtime MAY
offer a process-level allowlist (the Zeta runtime:
`zeta serve --connectors`).

### 13.1 The describe invocation

Invoked with the single argument `--describe`, a connector executable
MUST print exactly one JSON object to stdout and exit 0, without
requiring any credentials or network access:

```json
{
  "id": "slack",
  "protocol_versions": [0],
  "events": {"slack.message.received": { …json schema… },
             "slack.message.post": { …json schema… }},
  "filters": {"slack.message.received": { …json schema… }},
  "operations": [{"name": "slack.message.post",
                  "semantics": "connector_deduplicated",
                  "options_schema": { …json schema… }}],
  "settings": ["poll_interval"]
}
```

- `id` MUST equal the `<id>` under which the executable was found,
  and MUST equal the `name` in the connector's later `hello`.
- `events` maps every event type the connector can emit **or**
  deliver to its payload JSON Schema (or `null` for schemaless).
- `filters` maps ingress event types to the JSON Schema for the
  `filter` object an agent binding may carry.
- `operations` lists every operation the connector serves, each with
  its delivery `semantics` — one of `idempotent_with_key`,
  `connector_deduplicated`, `at_least_once`, `unsafe_to_retry` — and
  the JSON Schema for binding options.
- `settings` (informative) names the `hello_ack.config` keys the
  connector understands.

The runtime uses the manifest at project load for schema
registration and binding validation, records it in the project
snapshot, and compares manifests when re-executing a recorded
generation. The manifest is static metadata: `--describe` MUST NOT
read secrets, open connections, or touch project state.

### 13.2 The run invocation

Invoked with no arguments, the executable speaks wire-v0 on stdio as
a `source` child: settings arrive in `hello_ack.config`, secrets via
the environment (§5.7), ingress flows as `event`s, egress as `call`s
against its declared operations, and reconnection is crash-only
(§8.1).

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
