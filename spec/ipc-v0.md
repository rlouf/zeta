# Zeta inter-process protocol, version 0 (`ipc-v0`)

Status: normative.
Audience: anyone implementing a Zeta peer or runtime in any language.

The key words MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY are to be
interpreted as described in RFC 2119.

Conformance vectors live in `spec/vectors/ipc/`. An implementation MUST pass
the vectors that apply to its role.

## 1. Model

The protocol is a newline-delimited JSON-RPC 2.0 profile. A **peer** and a
**runtime** can both send requests after initialization. Logical roles control
which methods each side may use:

- A `source` peer publishes ingress events.
- A `client` peer queries and controls the runtime and receives committed-event
  notifications.
- A `provider` peer serves methods that it declares during initialization.

Roles describe permissions. They do not describe which process started the
other process. A peer may request more than one role.

The protocol defines message shapes, initialization, authorization,
correlation, flow control, and connection state. Method implementations and
process supervision are outside the protocol.

## 2. Transport and framing

Messages use UTF-8 newline-delimited JSON:

- One line contains exactly one JSON object.
- Writers MUST terminate every message with one `\n` byte and flush it.
- Readers MUST also accept one complete final JSON object at end-of-stream
  without a terminating newline.
- Empty lines are invalid.
- JSON-RPC batch arrays are not supported.
- A JSON line MUST NOT exceed 8 MiB, excluding its terminating newline.
- A raw newline cannot occur inside a JSON string. Writers escape it as `\n`.

A receiver MUST bound memory while reading malformed or oversized input. It
MUST surface a bounded violation and MUST NOT crash. It MAY send a JSON-RPC
error. The process supervisor decides whether repeated violations terminate a
process. When a parsed invalid value is unambiguously a request, the receiver
retains any valid request id for that error response.

JSON serialization is not identity-bearing. Readers accept ordinary valid
JSON. Writers emit compact JSON without insignificant whitespace. Object key
order has no protocol meaning.

## 3. JSON-RPC profile

Every message carries `"jsonrpc":"2.0"`. Unknown top-level members MUST be
ignored.

### 3.1 Request identifiers

A request `id` is a JSON string or an integral JSON number in the union of
`i64` and `u64`. It MUST NOT be `null`, a Boolean, a fraction, an array, or an
object.

Each sender owns its identifier namespace. Both sides may therefore have
request id `1` open at the same time. A sender MUST NOT reuse an identifier
while its earlier request remains unanswered.

An error response may use `id: null` only when the receiver cannot recover a
valid request identifier. A success response MUST carry the identifier of its
request.

### 3.2 Request

```json
{"jsonrpc":"2.0","id":1,"method":"session.list","params":{}}
```

`method` is a non-empty string. `params`, when present, is an object. Protocol
methods with no arguments carry `params: {}`. A request contains neither
`result` nor `error`.

### 3.3 Notification

```json
{"jsonrpc":"2.0","method":"event","params":{"event":{}}}
```

A notification is a request without `id`. The receiver MUST NOT answer a
notification. A notification contains neither `result` nor `error`.

### 3.4 Success response

```json
{"jsonrpc":"2.0","id":1,"result":{}}
```

A success response carries exactly one `result` member. JSON-RPC permits any
JSON result, including `null`. Protocol methods in this specification return
objects.

### 3.5 Error response

```json
{
  "jsonrpc":"2.0",
  "id":1,
  "error":{
    "code":-32000,
    "message":"Provider request failed",
    "data":{"code":"channel_not_found","retryable":false}
  }
}
```

An error response carries exactly one `error` member. `error.code` is an
integral `i64`, `error.message` is a string, and optional `error.data` is any
JSON value. It contains no `result` member.

Application and provider failures use the server-error range from `-32000` to
`-32099`. Their `data` object carries a stable string `code`. It also carries a
Boolean `retryable` when retry behavior is meaningful.

### 3.6 Standard errors

| code | message | use |
|---:|---|---|
| `-32700` | `Parse error` | malformed JSON |
| `-32600` | `Invalid Request` | invalid JSON-RPC shape or connection state |
| `-32601` | `Method not found` | unknown or unauthorized method |
| `-32602` | `Invalid params` | invalid protocol-method parameters |
| `-32603` | `Internal error` | unexpected implementation failure |

Malformed JSON and invalid requests use `id: null` when a valid id cannot be
recovered. A receiver does not send an error in response to an invalid
response or notification; it surfaces a local protocol violation instead.

## 4. Initialization

`initialize` is the first request from a peer to the runtime. No other request
or notification is valid before its successful response. A connection may
initialize only once.

### 4.1 Request

```json
{
  "jsonrpc":"2.0",
  "id":1,
  "method":"initialize",
  "params":{
    "protocol_versions":[0],
    "peer":{"name":"filesystem","version":"0.1.0"},
    "roles":["source","provider"],
    "event_types":[
      {"type":"file.created","schema":"file.created@1"}
    ],
    "methods":[{"name":"file.archive"}],
    "heartbeat_seconds":10,
    "max_in_flight":64
  }
}
```

The parameter members are:

| member | type | required | meaning |
|---|---|---|---|
| `protocol_versions` | non-empty array of non-negative integers | yes | versions the peer supports |
| `peer` | object | yes | non-empty string `name` and `version` |
| `roles` | non-empty array | yes | unique values from `source`, `client`, `provider` |
| `event_types` | array | for `source` | unique `{type, schema}` declarations |
| `methods` | array | for `provider` | unique `{name}` declarations |
| `heartbeat_seconds` | number | no | requested interval in `[1, 300]`; default `10` |
| `max_in_flight` | integer | no | requested request limit in `[1, 1024]`; default `64` |

Unknown initialization parameters are invalid. `peer` contains only `name` and
`version`. An event declaration contains only non-empty string `type` and
`schema` members. A method declaration contains only a non-empty string `name`.

A method declaration names the direct JSON-RPC method that the provider serves.
It MUST NOT use any of these names or namespaces:

- `initialize`, `event`, `ping`, or `shutdown`;
- a name beginning with `events.` or `session.`;
- a name beginning with the JSON-RPC-reserved `rpc.` prefix.

The runtime accepts every requested role or rejects initialization. It does
not select a subset, and role order has no meaning. An unsupported version
returns a server error with `data.code: "unsupported_version"` and
`data.retryable: false`.

### 4.2 Response

```json
{
  "jsonrpc":"2.0",
  "id":1,
  "result":{
    "protocol_version":0,
    "runtime":{"name":"zeta","version":"0.1.0"},
    "roles":["source","provider"],
    "config":{},
    "heartbeat_seconds":10,
    "max_in_flight":64
  }
}
```

The result states the selected version, runtime identity, accepted roles,
non-secret peer configuration, heartbeat interval, and per-side unanswered
request limit. `config` is always an object. Secrets reach a managed process
through its spawn-time environment and MUST NOT appear in protocol messages.

Traffic before initialization returns `-32600` with
`data.code: "not_initialized"`. A second `initialize` request returns `-32600`
with `data.code: "already_initialized"`.

## 5. Roles and methods

| method | direction | role or authority | result boundary |
|---|---|---|---|
| `initialize` | peer to runtime | uninitialized peer | establishes the connection |
| `events.publish` | peer to runtime | `source` | durable insert or duplicate resolution |
| `events.list` | peer to runtime | `client` | one cursor-ordered page |
| `event` | runtime to peer | `client`; notification | best-effort notice after commit |
| `session.start` | peer to runtime | `client` | queues a first session message |
| `session.send` | peer to runtime | `client` | queues an existing-session message |
| `session.status` | peer to runtime | `client` | one durable session projection |
| `session.list` | peer to runtime | `client` | the durable session catalog |
| `session.cancel` | peer to runtime | `client` | durable cancellation result |
| `ping` | either direction | initialized connection | proves liveness |
| `shutdown` | supervisor to managed process | process authority | acknowledges orderly stop |
| declared method | runtime to peer | `provider` | provider result or error |

An unknown method, a fixed method used without its required role, or an
undeclared provider method returns `-32601`. Process authority for `shutdown`
comes from the process relationship, not from a self-declared role.

## 6. Events

### 6.1 `events.publish`

The parameters use journal vocabulary:

```json
{
  "type":"file.created",
  "payload":{"path":"/p/inbox/todo.txt"},
  "idempotency_key":null,
  "caused_by":null,
  "session_id":null,
  "run_id":null,
  "turn_id":null
}
```

`type` and `payload` are required. `type` must be one of the source's declared
event types. `payload` is an object whose compact UTF-8 JSON is at most 64 KiB.
The other members are optional non-empty strings or `null`. Unknown members are
invalid.

The runtime derives the durable source from the initialized peer. A peer cannot
claim another source name. A source may provide an idempotency key. Runtime
configuration may instead derive the final key. Without either, a redelivery
creates a new durable event.

The result is:

```json
{"inserted":true,"event":{"id":"evt_...","type":"file.created"}}
```

The complete `event` value has the durable event shape in §6.3. The runtime
sends this success response only after it has inserted the event durably or
resolved it as a duplicate. A source advances an upstream cursor only after
receiving the response.

### 6.2 `events.list`

The parameter object accepts these optional filter members:

- `event_type`, `event_type_prefix`, `session_id`, `run_id`, `turn_id`, and
  `caused_by`: non-empty string or `null`;
- `after_cursor`: non-negative integer;
- `limit`: non-negative integer;
- `newest_first`: Boolean.

Unknown members are invalid. The result is
`{"events":[...],"next_cursor":<integer-or-null>}`. Results are ordered by
cursor according to the filter. A page may contain fewer events than requested
so its encoded response stays within the frame limit.

### 6.3 Durable event and `event` notification

A durable event has this external shape:

```json
{
  "id":"evt_...",
  "type":"file.created",
  "source":"filesystem",
  "payload":{},
  "idempotency_key":"...",
  "caused_by":null,
  "session_id":null,
  "run_id":null,
  "turn_id":null,
  "timestamp_ms":1786400000000,
  "cursor":42
}
```

`id`, `type`, and `source` are non-empty strings. `payload` is an object.
`timestamp_ms` is a non-negative integer containing Unix epoch time in
milliseconds. `cursor` is a non-negative integer. Identity and scope
members are non-empty strings or `null`. Readers ignore unknown durable-event
members.

The runtime sends committed events to every client as this notification:

```json
{"jsonrpc":"2.0","method":"event","params":{"event":{...}}}
```

The notification is best effort and has no acknowledgment. A client uses the
cursor to ignore duplicates, detect gaps, and recover through `events.list`.
Version 0 has no subscription method.

## 7. Sessions

The client methods have these strict parameter objects:

- `session.start`: required non-empty `message`; optional `idempotency_key`,
  which is a non-empty string or `null`.
- `session.send`: required non-empty `session_id` and `message`; optional
  `idempotency_key`, which is a non-empty string or `null`.
- `session.status`: required non-empty `session_id`.
- `session.list`: empty object.
- `session.cancel`: required non-empty `run_id`; optional `session_id` and
  `reason`, each a non-empty string or `null`.

Unknown members are invalid.

`session.start` and `session.send` return `event_id`, `queue_item_id`,
`agent_id`, `session_id`, `run_id`, and `status`. Idempotency keys stay stable
across reconnect and request replay.

`session.status` returns one durable session projection. `session.list` returns
`{"sessions":[...]}` using the same projection shape. `session.cancel` returns
the durable cancellation fields `cancelled`, `changed`, `run_id`,
`queue_item_id`, `session_id`, `status`, and `terminal_status`.

## 8. Direct provider methods

The runtime invokes a provider's declared method name directly:

```json
{
  "jsonrpc":"2.0",
  "id":"runtime-7",
  "method":"slack.message.post",
  "params":{
    "input":{"channel":"general","text":"Hello"},
    "effect_key":"effect-123"
  }
}
```

`input` is an object and `effect_key` is a non-empty string. No other parameter
is valid. The effect key remains stable across retries and process restarts.
The success result is an object. A provider failure is an error response whose
`data` object contains a stable `code` and Boolean `retryable`.

## 9. Correlation and flow control

Both sides may send requests after initialization. Responses may arrive out of
order. Each side maintains separate incoming and outgoing request tables.

Each side may have at most the negotiated `max_in_flight` unanswered outgoing
requests. A success or error response frees one slot. A duplicate outstanding
request id is an invalid request. A response for an unknown id is a local
protocol violation.

Every received request receives exactly one success or error response unless
the connection closes first. Notifications create no pending state.

## 10. Liveness and shutdown

After an idle `heartbeat_seconds` interval, a supervisor sends `ping {}` as a
request. The peer answers with `{}`. Any valid incoming message proves
liveness. Three unanswered heartbeat intervals permit the supervisor to
declare a managed process dead.

`shutdown {"reason":<string-or-null>}` is a request. The managed process stops
accepting new work, responds with `{}`, and exits. Only the process supervisor
may send it. Process respawn, backoff, and signal escalation are outside the
protocol.

## 11. Executable peers

A connector is an executable that initializes with `source`, `provider`, or
both roles. Discovery uses the shell:

- `zeta-connector-<id>` on `PATH` identifies connector `<id>`;
- an executable in a project's `agents/connectors/` directory is local to that
  project and uses its file name, without an extension, as `<id>`.

Invoked with `--describe`, the executable prints one JSON manifest to stdout
and exits successfully without credentials, network access, or project-state
changes. The manifest declares connector identity, event schemas, filters,
provider operations, delivery semantics, option schemas, and setting names.

Invoked with no arguments, the executable uses this protocol on stdin and
stdout. It writes diagnostics only to stderr. Any non-protocol stdout data is a
framing violation.

## 12. Versioning

Version 0 has these extension rules:

- Readers ignore unknown JSON-RPC message members.
- Fixed protocol-method parameter objects are strict.
- Durable-event readers ignore unknown members.
- Unknown methods return `-32601`.
- Initialization chooses the highest version supported by both sides.
- A connection uses the selected version until it closes.

## Appendix A — source/provider session

Direction markers are informative and are not transmitted:

```text
peer -> runtime: initialize request
runtime -> peer: initialize result
peer -> runtime: events.publish request id 1
runtime -> peer: declared provider request id 1
peer -> runtime: events.publish request id 2
peer -> runtime: provider result id 1
runtime -> peer: durable success result id 2
runtime -> peer: durable success result id 1
runtime -> peer: ping request
peer -> runtime: ping result
runtime -> peer: shutdown request
peer -> runtime: shutdown result
```

The executable transcript is `spec/vectors/ipc/sessions/source-provider.jsonl`.
