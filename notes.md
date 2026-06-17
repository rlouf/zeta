# Sigil active execution plan

## Active order

1. Finish the Zeta durable timeline cleanup by removing the trace `run_event`
   chain.
2. Make the Zeta RPC syscall surface explicit and enforceable.
3. Make every tool a declared capability and mediate all model-to-tool calls
   through that capability interface.
4. Replace ad hoc runtime dictionaries with typed Zeta domain records.
5. Make model input/output provider-neutral inside Zeta.
6. Split prompt construction into plan, commit, and provider render phases.
7. Rework the agent loop into an explicit resumable step engine.
8. Promote replay/diff/fork invariants into acceptance tests.
9. Sharpen the `src/zeta` core boundary from Sigil integration.

Do not start a later section until the previous section is complete or Remi
explicitly changes the order.

## Architecture guide

Use this as the north-star shape while implementing the plan. Keep the diagram
simple and stable; each section below should fit into it rather than inventing a
parallel runtime shape.

```text
                RPC / CLI / Sigil
                      |
                      v
              +----------------+
              |  Run Request   |
              +----------------+
                      |
                      v
              +----------------+
              |  RunState      |
              |  Step Engine   |
              +----------------+
                 |          |
                 |          v
                 |   +----------------+
                 |   | Capability     |
                 |   | Projection     |
                 |   +----------------+
                 |          |
                 |          v
                 |   +----------------+
                 |   | Capability     |
                 |   | Registry       |
                 |   +----------------+
                 |
                 v
        +--------------------+
        | Prompt Plan        |
        | pure, typed        |
        +--------------------+
                 |
                 v
        +--------------------+
        | Prompt Commit      |
        | objects/refs/deriv |
        +--------------------+
                 |
                 v
        +--------------------+
        | ModelInput         |
        | provider-neutral   |
        +--------------------+
                 |
                 v
        +--------------------+
        | Provider Adapter   |
        | chat/responses     |
        +--------------------+
                 |
                 v
        +--------------------+
        | ModelOutput        |
        | provider-neutral   |
        +--------------------+
                 |
                 v
              RunState
```

Every step may write durable facts:

```text
+-------------------+     +-------------------+
| Trace Store       |     | Event Store       |
| objects           |     | ordered events    |
| refs              |     | cursors           |
| derivations       |     | causality         |
+-------------------+     +-------------------+

Objects = durable values.
Events = what happened.
Derivations = why a value exists.
Refs = mutable names for current values.
```

Implementation rule:

```text
External JSON/RPC/provider payloads
        |
        v
typed Zeta records
        |
        v
step engine changes RunState
        |
        v
events record what happened
objects record durable values
provider adapters are the only provider-specific layer
capability projection is the only model-to-tool lookup layer
```

## 1. Remove the trace `run_event` chain

### Current state

The durable-event source-of-truth work is mostly implemented:

- `events.seq` exists and `list_events()` orders by `seq`.
- `current_timeline()` reads from the durable event reader first.
- `timeline_from_events()` projects durable events back into timeline events.

The remaining cleanup is to stop maintaining the old trace linked list used as a
parallel timeline.

### Behavior to preserve

- `record_event()` still returns the normalized payload with `id`, `time`,
  `cwd`, and `session`.
- Trace objects remain the provenance/content store for prompts, assistant
  messages, tool calls/results, links, refs, and derivations.
- Chat rendering keeps user/assistant/tool-call/tool-result handling,
  proposed-effect suppression, and truncated-arg repair.
- Trace CLI commands that inspect content-addressed objects keep working.

### Tests first

Add or adjust tests proving a fresh session:

- projects user messages, model calls, tool calls, tool results, aborts, and
  usage from the durable event log alone;
- writes no `run_event` objects;
- writes no `run/<id>/head` or `run/<id>/event_head` refs;
- preserves current timeline projection and rendering behavior.

### Implementation steps

1. Stop `record_event()` from writing the trace `run_event` linked list and
   advancing run/event-head refs.
2. Keep durable event append as the ordering and causality source.
3. Run `ripple` before deleting each now-dead Python function. Expected
   candidates:
   - `timeline_events_from_head`
   - `timeline_from_ref`
   - `timeline_from_object`
   - `rehydrated_model_event`
   - `should_update_run_head`
   - `run_head_ref`
   - `event_head_ref`
4. Delete only after callers and tests are accounted for.
5. Remove the trace fallback in `current_timeline()` once tests prove durable
   projection parity for new sessions.

### Verification

- `uv run pytest tests/test_zeta_trace.py tests/test_security_state.py -q`
- `uv run pytest -q`
- `uvx --with radon radon cc src tests -s`

### Follow-up decision

After the chain is removed, decide whether to introduce a shared
connection/unit-of-work between `SqliteEventStore` and `SqliteStore`, or keep
explicit orphan tolerance plus repair tooling. Removing the chain makes that
atomicity decision easier to reason about.

## 2. Zeta RPC syscall surface

### Second opinion status

- Tried `claude -p` as required for a complex protocol/refactor plan.
- The command produced no output after about 90 seconds and had to be killed.
  No usable external critique is folded in here.

### Current read

The current RPC layer already has the start of a kernel-like syscall surface:

- `initialize` returns server metadata and protocol version.
- `session.run` runs a synchronous agent turn.
- `tools.register` registers client-provided tools on the server registry.
- `tools.call` is emitted by the server when a registered client tool is
  invoked.
- `tools.respond` lets the client return the result of a prior `tools.call`.
- `events.publish` streams live persisted runtime events to the client.

The missing piece is a stable protocol contract with identities, cursors,
cancellation, trace references, capability metadata, and structured errors.

### Behavior to preserve

- Existing `zeta rpc --stdio` and `sigil zeta rpc --stdio` initialization keep
  working.
- Existing `session.run` callers that pass only `objective`, `tools`, and
  `context` keep receiving `outcome` and `final_text` until Remi decides to
  remove that compatibility.
- Zeta remains product-neutral. `src/zeta` must not import `sigil`.
- Client tools remain optional. A pure session with no tools should still run.
- Events remain persisted before they are published.

### Smallest design improvement

Make the RPC protocol explicit and typed enough that clients can treat Zeta as a
small runtime kernel:

- `session.run` creates an addressable run.
- events and trace refs let clients inspect what happened.
- cancellation targets a concrete run.
- tools are registered as capabilities with declared effects and execution
  semantics.
- errors are structured and stable.

### Slice 1: protocol contract and golden tests

- Add or update a protocol document that defines:
  - protocol version and compatibility policy;
  - request/response envelope shape;
  - notifications vs requests;
  - method list and schemas;
  - stable error codes;
  - event ordering and cursor semantics;
  - tool capability metadata.
- Add golden tests for `initialize`, unknown methods, invalid params, and the
  current happy path.
- If adding a new docs file is necessary, ask Remi first.

Verification:

- `uv run pytest tests/test_zeta_agent.py -q`
- `uv run pre-commit run --all` if documentation changed.

### Slice 2: structured RPC errors

- Introduce a narrow internal error type for RPC dispatch with JSON-RPC code,
  Zeta error code, message, and optional data.
- Replace generic `-32000` stringification for expected protocol errors.
- Keep unexpected exceptions grouped under one internal error code.

Test cases:

- unknown method;
- missing `objective`;
- invalid `workflow`;
- duplicate tool registration;
- invalid tool schema once schema validation lands.

Verification:

- `uv run pytest tests/test_zeta_agent.py -q`

### Slice 3: run identity in `session.run`

- Generate a stable `run_id` at the start of `session.run`.
- Include `run_id` in:
  - the persisted user event payload;
  - every event published during that run;
  - the final `session.run` result.
- Prefer a new `run_*` id because it names the RPC execution rather than one
  event.
- Return at least:
  - `run_id`;
  - `outcome`;
  - `final_text`;
  - final event cursor if available.
- Ensure `run_id` can be indexed or otherwise filtered efficiently before
  adding `events.list(run_id=...)`.

Test cases:

- all `events.publish` notifications for a run carry the same `run_id`;
- sequential runs get distinct ids;
- aborted runs still return their `run_id`.

Verification:

- focused RPC tests in `tests/test_zeta_agent.py`
- `uv run pytest -q`

### Slice 4: event cursor exposure

- Expose the durable event cursor in published events and `session.run` result.
- Add `events.list`.
- Use durable `seq` cursor rather than timestamp/id ordering.

Method shape:

- params: `after`, `limit`, optional `session_id`, optional `run_id`;
- result: `events`, `next_cursor`.

Test cases:

- `events.list` returns events in append order;
- `after` resumes without duplication;
- `limit` returns a stable `next_cursor`;
- filtering by run/session does not reorder events.

Verification:

- `uv run pytest tests/test_zeta_agent.py tests/test_zeta_trace.py -q`

### Slice 5: trace refs in run result

- Decide the minimal trace identity clients need:
  - prompt trace ids;
  - event head ref if still relevant after the trace-chain cleanup;
  - prompt object ids;
  - returned object ids for tool results.
- Start with a small `trace` object in `session.run` result:
  - `prompt_ids`;
  - maybe `model_event_ids` and `tool_event_ids`.
- Avoid a broad trace query API in this slice.

Test cases:

- a pure answer run returns trace refs;
- a tool run returns refs for model/tool events;
- missing trace store data degrades gracefully.

Verification:

- `uv run pytest tests/test_zeta_agent.py tests/test_zeta_trace.py -q`

### Slice 6: capability contract for `tools.register`

- Validate registered tool schemas with `Draft202012Validator.check_schema`.
- Extend capability metadata to include:
  - `effects`;
  - whether staging is supported;
  - whether direct execution is allowed;
  - whether the tool is interactive;
  - optional timeout/default timeout.
- Keep the execution decision in Zeta:
  - read-only tools may run under staged workflows;
  - mutating tools must stage unless direct mode is active and the capability
    permits direct execution.

Test cases:

- invalid schemas are rejected at registration;
- undeclared effects are treated as mutating or refused;
- a mutating client tool without staging support is refused in propose mode;
- duplicate registration is rejected unless the contract explicitly allows
  re-registering the same client-owned capability.

Verification:

- `uv run pytest tests/test_zeta_tools.py tests/test_zeta_agent.py -q`

### Slice 7: tool call lifecycle hardening

- Add stable call identity and lifecycle states around client tool calls:
  - `requested`;
  - `responded`;
  - `failed`;
  - `cancelled`;
  - `timed_out`.
- Add timeout handling for blocking `tools.call`.
- Add structured response validation for `tools.respond`.
- Keep streaming tool output out of scope unless a concrete client needs it.

Test cases:

- client disconnect returns a structured tool error;
- timeout returns a structured tool error;
- late/unknown `tools.respond` is handled predictably;
- malformed tool result is normalized.

Verification:

- `uv run pytest tests/test_zeta_agent.py tests/test_zeta_tools.py -q`

### Slice 8: `session.cancel`

Split cancellation into two sub-slices because active cancellation cannot work on
a single synchronous stdio loop.

First:

- add an in-memory run table keyed by `run_id`;
- thread the existing `cancellation_event` support from `run_agent_turn` through
  `run_rpc_session`;
- add `session.cancel` result semantics for unknown/completed runs.

Then:

- make `session.run` run in a worker so the server can process `session.cancel`
  while a run is active;
- audit `JsonRpcServer` writes and shared `tool_responses` before making the
  server concurrent.

`session.cancel` shape:

- params: `run_id`, optional `reason`;
- result: `cancelled: true/false`, `run_id`.

Test cases:

- cancelling an active run publishes a `turn_aborted` event;
- cancelling an unknown/completed run returns a stable structured result;
- deadline and explicit cancellation produce different reasons.

Verification:

- `uv run pytest tests/test_zeta_agent.py -q`

### Slice 9: optional event subscription

- Add `events.subscribe` only after `events.list` exists.
- For stdio, keep it simple: subscribe records a cursor/filter and causes future
  persisted events to be published to that client.
- Do not build fanout, sockets, or durable subscriptions yet.

Test cases:

- subscribe after cursor receives only later events;
- subscription filters by session/run;
- unsubscribed clients receive no extra notifications.

Verification:

- `uv run pytest tests/test_zeta_agent.py -q`

## 3. Capabilities and mediation

### Second opinion status

- Tried `gemini -p` for the complex capability/refactor plan.
- Gemini opened an authentication prompt instead of returning an answer, so it
  was killed. No usable external critique is folded in here.

### Decision

Adopt the greenfield design. Broad renames are allowed and there is no need to
preserve the current `ToolSpec` / `ToolImpl` / `ToolRegistry` API.

The runtime primitive is a **capability**. "Tool" is only used at external
protocol boundaries where the model provider uses that word, such as Chat
Completions `tools` and model-emitted `tool_calls`.

Rename runtime concepts directly:

- `ToolSpec` -> `CapabilitySpec`
- `ToolImpl` -> `Capability`
- `ToolRegistry` -> `CapabilityRegistry`
- `allowed_tools` -> `allowed_capabilities`
- `model_tool_descriptors` -> `model_capability_descriptors`
- internal `tool_call` vocabulary -> `capability_call` where it is not mirroring
  an external provider protocol.

### Behavior to preserve

- Built-in Sigil capabilities keep their current model-visible aliases, schemas,
  effects, and staging behavior.
- `ask`, `propose`, and `do` keep their current execution semantics:
  read/search capabilities run; mutating capabilities stage in propose;
  mutating capabilities run directly in do.
- Zeta still does not import Sigil.
- Client capabilities registered over RPC remain available to the model through
  the same projection mechanism as in-process capabilities.

### Target shape

Use provider-qualified identity from the start:

```python
@dataclass(frozen=True)
class CapabilityId:
    provider: str
    name: str

    def canonical(self) -> str:
        return f"{self.provider}.{self.name}"
```

```python
@dataclass(frozen=True)
class CapabilitySpec:
    id: CapabilityId
    description: str
    input_schema: dict[str, Any]
    effects: tuple[EffectKind, ...]
    aliases: tuple[str, ...] = ()
    interactive: bool = False
```

```python
@dataclass(frozen=True)
class CapabilityPolicy:
    supports_staging: bool
    supports_direct: bool
    trust: TrustLevel
    timeout_seconds: float | None = None
```

```python
class CapabilityExecutor(Protocol):
    def invoke(
        self,
        capability: CapabilitySpec,
        params: dict[str, Any],
        *,
        mode: ExecutionMode,
    ) -> CapabilityResult:
        ...
```

```python
@dataclass(frozen=True)
class Capability:
    spec: CapabilitySpec
    policy: CapabilityPolicy
    executor: CapabilityExecutor
```

`CapabilityRegistry` is keyed by `CapabilityId.canonical()`, not by
model-visible alias.

Examples:

- `sigil.read`
- `sigil.bash`
- `sigil.edit`
- `rpc.<client-id>.read`
- `mcp.<server-id>.read`

Multiple providers may register the same local name. They may not register the
same canonical capability id.

### Model projection

The model sees a per-run projection, not the global registry:

```python
@dataclass(frozen=True)
class CapabilityProjection:
    alias_to_id: dict[str, str]
    descriptors: list[dict[str, Any]]
```

Before each run:

1. Resolve `allowed_capabilities` to canonical capability ids.
2. Choose model-visible aliases for those capabilities.
3. Reject ambiguous aliases unless the host explicitly selects qualified aliases.
4. Build provider-specific descriptors from the projection.
5. Store the projection on the run so tool-call replay is deterministic.

When the model calls alias `read`, Zeta resolves it through the projection:

```python
capability_id = projection.alias_to_id["read"]
result = registry.invoke(capability_id, args, mode=config.execution_mode)
```

For the first implementation, existing Sigil capabilities use provider `sigil`,
canonical ids like `sigil.read`, and aliases matching today's model-visible
names: `read`, `bash`, `edit`, and so on.

### Slice 1: rename primitives and lock the boundary

- Replace runtime names directly:
  - `ToolSpec` -> `CapabilitySpec`
  - `ToolImpl` -> `Capability`
  - `ToolRegistry` -> `CapabilityRegistry`
  - `allowed_tools` -> `allowed_capabilities`
  - `model_tool_descriptors` -> `model_capability_descriptors`
- Keep external protocol names only where the provider contract requires them.
- Add or update tests that assert:
  - `src/zeta` does not import `sigil`;
  - Sigil built-ins register as `sigil.*` capabilities;
  - schemas are valid Draft 2020-12;
  - effects are declared;
  - current model-visible names are aliases;
  - mutating capabilities have staging support where propose mode relies on it;
  - read/search capabilities are recognized as non-mutating.

Verification:

- `uv run pytest tests/test_zeta_tools.py tests/test_zeta_agent.py -q`

### Slice 2: implement capability data model

- Add `CapabilityId`, `CapabilitySpec`, `CapabilityPolicy`,
  `CapabilityExecutor`, `Capability`, `CapabilityResult`, and `TrustLevel`.
- Make provider identity mandatory.
- Make staging/direct behavior policy, not callable shape.
- Executors receive `mode="stage" | "direct"` and may return a structured
  unsupported result when the mode is unsupported.
- Do not allow arbitrary dict results to leak past executor boundaries.

Test cases:

- existing Sigil capabilities get the expected id/spec/policy;
- two capabilities with the same provider-local name but different providers
  can coexist in the registry;
- duplicate canonical capability ids are rejected;
- RPC capabilities get client trust and declared execution support;
- invalid combinations are rejected, such as mutating + no direct + no stage.

Verification:

- `uv run pytest tests/test_zeta_tools.py -q`

### Slice 3: implement `CapabilityRegistry`

- Replace `ToolRegistry` with `CapabilityRegistry`.
- Methods:
  - `register(capability)`;
  - `get(capability_id)`;
  - `list_ids()`;
  - `project(enabled_ids, alias_overrides=None)`;
  - `invoke(capability_id, params, mode=...)`.
- `project()` returns `CapabilityProjection`.
- `invoke()` is the only place that applies execution-mode policy.

Test cases:

- duplicate canonical ids are rejected;
- alias resolution rejects duplicate aliases in one run;
- alias resolution can expose qualified aliases when the host selects them;
- model tool descriptors can be generated entirely from capability metadata.

Verification:

- `uv run pytest tests/test_zeta_tools.py tests/test_zeta_agent.py -q`

### Slice 4: add executor adapters

- Implement two adapters using existing code:
  - in-process adapter wraps current `run`/`stage` callables;
  - RPC-client adapter wraps the existing `JsonRpcServer.call_client_tool`.
- Existing Sigil built-ins become `Capability` instances with in-process
  executors.
- RPC-registered capabilities become `Capability` instances with RPC-client
  executors.

Test cases:

- in-process read/search tool invokes through the adapter;
- in-process mutating tool stages under propose mode;
- RPC client tool invokes through the same registry path.

Verification:

- `uv run pytest tests/test_zeta_agent.py tests/test_zeta_tools.py -q`

### Slice 5: update the agent loop to use projections

- `AgentConfig` uses `allowed_capabilities`.
- Before each run, build a `CapabilityProjection` from the registry and allowed
  capability ids.
- The provider/model API still receives provider-specific `tools` descriptors.
- Model-emitted `tool_calls` are resolved through `projection.alias_to_id`.
- The agent records capability ids and model aliases so replay/debug is
  deterministic.
- Execution-mode policy remains centralized in `CapabilityRegistry.invoke()`:
  - unknown capability -> structured error result;
  - invalid args -> structured error result;
  - mutating + stage mode + no staging support -> refused;
  - direct mode + no direct support -> refused;
  - timeout -> structured error result;
  - adapter exception -> structured error result.

Test cases:

- model cannot call unallowed capability even if registered;
- model alias resolves to the expected canonical id;
- invalid args are rejected before executor invocation;
- propose/do behavior is identical for built-ins and RPC tools with matching
  capability declarations.

Verification:

- `uv run pytest tests/test_zeta_agent.py tests/test_zeta_tools.py -q`
- `uv run pytest -q`

### Slice 6: make RPC register capabilities

- Update RPC registration parsing to accept the full capability declaration:
  - provider id if the server allows client-selected provider namespaces;
  - name;
  - description;
  - input schema;
  - effects;
  - aliases;
  - supports_staging;
  - supports_direct;
  - interactive.
- Server assigns `trust = "client"` for RPC tools by default.
- Server should assign or validate the client provider namespace so one client
  cannot overwrite another provider's capability ids.
- Do not let a client claim `"kernel"` or `"host"` trust.
- Validate schema and policy at registration time.

Test cases:

- valid capability registration returns normalized capability declaration;
- valid client registration with name `read` can coexist with `sigil.read`
  under a different provider-qualified id;
- missing/invalid schema is rejected;
- client cannot spoof privileged trust;
- duplicate capability ids are rejected unless explicitly re-registering the
  same client-owned capability is part of the chosen contract;
- duplicate model aliases are rejected at run projection time, not at global
  registration time.

Verification:

- `uv run pytest tests/test_zeta_agent.py tests/test_zeta_tools.py -q`

### Slice 7: normalize capability results and errors

- Define one `CapabilityResult` shape:
  - `ok: true/false`;
  - `content`;
  - `metadata`;
  - `effect`;
  - `error: {code, message, data?}`.
- Normalize every executor result into this shape before the agent records it.
- Keep existing display summarizers working by preserving fields they read.

Test cases:

- malformed RPC tool result becomes a structured error;
- executor exceptions become structured errors;
- existing bash/read/edit result summaries still render correctly.

Verification:

- `uv run pytest tests/test_display.py tests/test_zeta_agent.py tests/test_zeta_tools.py -q`

### Slice 8: optional trust policy

- Only implement trust enforcement after declarations and mediation are stable.
- Initial policy can be conservative:
  - `kernel`: Zeta-owned internal capabilities only, likely none for now;
  - `host`: Sigil/in-process host tools;
  - `client`: current RPC peer tools;
  - `remote`: future network/service tools.
- Decide what trust gates:
  - direct execution of mutating effects;
  - access to filesystem-like effects;
  - whether a tool can be available in `do`;
  - whether a capability can be auto-included when `allowed_capabilities is None`.

Test cases:

- a low-trust mutating capability is not auto-enabled;
- explicit allow-list can enable it only under permitted execution modes;
- model descriptors omit refused capabilities.

Verification:

- `uv run pytest tests/test_zeta_agent.py tests/test_zeta_tools.py -q`

### Slice 9: future transport adapters

- Do not build MCP or remote process support yet.
- Once in-process and RPC-client tools both use the same mediation path, add a
  small spike adapter for one future transport only if needed.

Acceptance test for any new transport:

- no changes to `zeta.agent`;
- no changes to model descriptor generation;
- only registry registration and adapter invocation code changes.

## 4. Typed Zeta domain records

### Second opinion status

- Tried `claude -p` as required for a complex refactor plan.
- The installed Claude CLI is not authenticated and returned
  `401 Invalid authentication credentials`. No usable external critique is
  folded in here.

### Current read

The current runtime passes model messages, events, tool calls, telemetry, RPC
payloads, and trace fields as `dict[str, Any]`. This was useful for the
prototype, but it makes provider translation, replay, and refactors harder than
they need to be.

### Behavior to preserve

- Existing public RPC payloads keep working until their replacement is explicitly
  planned.
- Existing prompt reconstruction, timeline rendering, display summaries, and
  trace CLI behavior keep working.
- The trace store still persists JSON-compatible values.
- Tests continue to inspect JSON-like payloads at process and protocol
  boundaries.

### Smallest design improvement

Introduce typed runtime records inside `src/zeta`, while keeping JSON dicts only
at boundaries:

- RPC request/response parsing;
- provider adapters;
- SQLite serialization;
- model-visible tool/capability descriptors;
- test fixtures that intentionally assert wire format.

Initial records:

- `UserMessage`
- `AssistantMessage`
- `ToolCall`
- `ToolResult`
- `ModelTurn`
- `RuntimeEvent`
- `PromptComponent`
- `StoredObject`
- `RunId` / `EventId` / `ObjectId` wrappers where they clarify ownership.

### Tests first

Add tests for round-tripping each record through its boundary representation:

- runtime record -> provider/RPC/store dict;
- provider/RPC/store dict -> runtime record;
- invalid dicts fail with structured errors instead of leaking partial state.

### Implementation steps

1. Add the typed records next to their current owners, not in a broad `common`
   module.
2. Start with leaf records whose conversion is local:
   - model messages and tool/capability calls;
   - capability results;
   - runtime events.
3. Convert `run_agent_turn()` internals to use records while preserving current
   return payloads.
4. Convert prompt components after model/tool records are stable.
5. Convert RPC parsing last, because it is the externally visible contract.
6. Delete dict-shape helper functions only after `ripple` shows no remaining
   callers.

### Slice 1: typed tool-call records inside `zeta.agent` - complete

Start with model-emitted tool calls because the conversion is local to
`src/zeta/agent.py` and already has a small internal record,
`CapabilityCallInvocation`.

Target records:

- `ModelToolCall`
  - provider call id;
  - model-visible alias/name;
  - raw JSON argument string;
  - parsed params;
  - parse error.
- `CapabilityCallInvocation`
  - canonical capability id after projection validation;
  - `ModelToolCall`;
  - event serialization for the current trace/timeline boundary.

Behavior to preserve:

- malformed tool-call payloads still produce the current `invalid-tool-call`
  result;
- invalid JSON arguments still produce `invalid-json-args`;
- model aliases still resolve through the per-run projection;
- tool-call and tool-result events keep their current JSON shape, including
  `name`, `tool_call_id`, `arguments`, `input`, `capability_id`, and causality.

Tests first:

- round-trip a valid provider tool-call dict through `ModelToolCall` and back
  to the existing event dict;
- invalid function payload fails without creating a partial invocation;
- invalid JSON arguments preserve the current error message;
- alias resolution still records canonical `capability_id` on call and result
  events.

Implementation notes:

- Keep public function boundaries accepting/returning dicts in this slice.
- Convert dicts to records immediately inside `handle_tool_call()` and
  `model_tool_call_event()`.
- Do not change provider request/response adapters yet.

Verification:

- `uv run pytest tests/test_zeta_agent.py -q` passed with 79 tests.
- `uv run pytest tests/test_zeta_agent.py tests/test_zeta_tools.py -q` passed
  with 152 tests and 2 skipped.
- `uv run pytest -q` passed with 792 tests and 4 skipped.
- `uv run coverage run -m pytest` and `uv run coverage report` passed with
  93% total coverage.

### Slice 2: typed capability result payloads - complete

The registry already normalizes executor output into one JSON result shape.
Make that shape explicit without changing builtin or RPC wire payloads.

Target records:

- `CapabilityResultPayload`
  - `ok`;
  - `content`;
  - `metadata`;
  - `effect`;
  - `error`.
- `CapabilityError`
  - `code`;
  - `message`;
  - optional `data`.

Behavior to preserve:

- `CapabilityRegistry.invoke()` still returns dicts at its public boundary;
- existing display summaries still read `content`, `metadata`, `effect`, and
  `error`;
- malformed executor results and executor exceptions keep the structured
  errors added in Section 3.

Tests first:

- round-trip success, proposed-effect, and structured-error result payloads;
- malformed result normalization produces `CapabilityError`;
- display summaries for bash/read/edit still render from the boundary dict.

Verification:

- `uv run pytest tests/test_display.py tests/test_zeta_tools.py -q` passed
  with 135 tests and 2 skipped.
- `uv run pytest tests/test_zeta_agent.py tests/test_zeta_tools.py -q` passed
  with 156 tests and 2 skipped.
- `uv run pytest -q` passed with 796 tests and 4 skipped.
- `uv run coverage run -m pytest` and `uv run coverage report` passed with
  93% total coverage.

### Slice 3: typed runtime event records at the agent boundary - complete

Introduce typed records for the runtime events the agent creates before they are
persisted or published.

Target records:

- `ModelRuntimeEvent`;
- `ToolCallRuntimeEvent`;
- `ToolResultRuntimeEvent`;
- `TurnAbortedRuntimeEvent`.

Behavior to preserve:

- `AgentTurnResult.events` remains `list[dict[str, Any]]` for now;
- `event_sink` still receives dict payloads;
- trace attachment fields remain unchanged;
- durable timeline projection keeps current event shapes.

Tests first:

- current model/tool/abort event helper tests assert record-to-dict output;
- event sink receives the same dicts as before;
- trace object ids are still attached to the same events.

Verification:

- `uv run pytest tests/test_zeta_agent.py tests/test_zeta_trace.py -q` passed
  with 163 tests.
- `uv run pytest -q` passed with 801 tests and 4 skipped.
- `uv run coverage run -m pytest` and `uv run coverage report` passed with
  93% total coverage.

### Slice 4: typed model turn record without provider-neutralizing yet - complete

Make `ModelTurn` carry a typed assistant message wrapper while keeping
Chat-Completions-shaped dicts at the model adapter boundary. This is a local
preparation step for Section 5, not the provider-neutral model contract itself.

Target records:

- `AssistantMessage`
  - content;
  - reasoning content;
  - tool calls;
  - provider/raw dict for boundary preservation.
- `ModelTurn`
  - `AssistantMessage`;
  - streamed content flag;
  - telemetry;
  - prompt trace.

Behavior to preserve:

- `request_assistant_message()` still returns the same tuple until Section 5;
- prompt trace stores the same assistant-message object;
- final text extraction and tool-call extraction behave identically.

Tests first:

- assistant content-only response round-trips to current model event;
- assistant with tool calls round-trips to current tool call handling;
- reasoning content is preserved in the model event.

Verification:

- `uv run pytest tests/test_zeta_agent.py tests/test_zeta_model.py -q` passed
  with 144 tests.
- `uv run pytest -q` passed with 805 tests and 4 skipped.
- `uv run coverage run -m pytest` and `uv run coverage report` passed with
  93% total coverage.

### Slice 5: prompt component boundary records - complete

`PromptComponent` already exists. Tighten its boundary conversions before
attempting broader prompt-plan work.

Target records:

- explicit conversion helpers for component message/data dicts;
- typed wrappers for source event links where that improves ownership.

Behavior to preserve:

- prompt reconstruction remains byte-for-byte compatible for existing tests;
- trace object ids and derivation links remain unchanged;
- prompt component order remains stable.

Tests first:

- existing prompt reconstruction tests keep passing;
- add focused round-trip tests for a user message component, assistant message
  component, tool-call component, and tool-result component.

Verification:

- `uv run pytest tests/test_zeta_prompt.py tests/test_zeta_trace.py -q` passed
  with 142 tests.
- `uv run pytest -q` passed with 809 tests and 4 skipped.
- `uv run coverage run -m pytest` and `uv run coverage report` passed with
  93% total coverage.

### Slice 6: cleanup and caller audit

Only after the typed records above are in place:

- run `ripple` on old dict-shape helpers such as `assistant_tool_calls()`,
  `model_tool_call_event()`, `tool_result_event()`, and any conversion helpers
  replaced by records;
- delete helpers with no production callers;
- keep compatibility helpers where tests or public boundaries intentionally
  assert JSON shape.

Verification:

- `uv run pytest tests/test_zeta_agent.py tests/test_zeta_tools.py tests/test_zeta_prompt.py -q`
- `uv run pytest -q`
- `uvx --with radon radon cc src tests -s`

### Verification

- `uv run pytest tests/test_zeta_agent.py tests/test_zeta_tools.py tests/test_zeta_prompt.py -q`
- `uv run pytest -q`
- `uvx --with radon radon cc src tests -s`

## 5. Provider-neutral model input and output

### Current read

Zeta currently keeps Chat Completions-shaped messages internally and translates
Responses API payloads at the wire boundary. That means provider compatibility
concerns leak into prompt building, replay, and trace objects.

### Behavior to preserve

- Current Chat Completions and Codex Responses calls keep working.
- Responses reasoning replay still preserves raw provider output items when the
  provider requires them.
- Tool/capability calls remain visible to the model with provider-compatible
  names and schemas.
- Token usage and finish reasons remain available in telemetry and trace.

### Target shape

Define a provider-neutral internal model contract:

- `ModelInput`
  - instructions/system content;
  - ordered conversation items;
  - capability descriptors;
  - tool choice;
  - max output tokens;
  - selected model;
  - thinking/reasoning policy;
  - prompt cache/session key.
- `ModelOutput`
  - text content;
  - reasoning summary/content;
  - capability calls;
  - finish reason;
  - usage;
  - provider request/response metadata;
  - opaque provider replay items.

Provider adapters translate:

- `ModelInput` -> Chat Completions request;
- `ModelInput` -> Responses request;
- Chat Completions response -> `ModelOutput`;
- Responses stream -> `ModelOutput`.

### Tests first

- Golden tests for Chat Completions request rendering.
- Golden tests for Responses request rendering.
- Stream accumulator tests that produce `ModelOutput`, not an internal
  Chat-Completions-shaped dict.
- Replay tests proving Responses opaque items are preserved across turns.

### Implementation steps

1. Introduce `ModelInput`, `ModelOutput`, `ModelUsage`, and `ModelFinishReason`.
2. Make current chat-completions request/response conversion an adapter.
3. Make current responses request/stream conversion an adapter.
4. Update `request_assistant_message()` to return `ModelOutput`.
5. Update prompt tracing to store provider-neutral model output plus adapter
   metadata.
6. Keep compatibility shims only at RPC/test boundaries until all callers are
   migrated.

### Verification

- `uv run pytest tests/test_zeta_model.py tests/test_zeta_responses.py -q`
- `uv run pytest tests/test_zeta_agent.py tests/test_zeta_prompt.py -q`
- `uv run pytest -q`

## 6. Prompt plan, commit, and render

### Current read

`PromptBuilder.build()` both assembles prompt components and stores trace
objects when a store is available. That makes it harder to dry-run prompt
construction, diff prompt inputs, compare provider renders, or replay a prompt
without side effects.

### Behavior to preserve

- Prompt component order remains a public prefix-cache contract:
  system prompt, tool descriptors, project context, then volatile components.
- Content-addressed prompt components, prompt objects, derivations, and prompt
  refs keep their current provenance role.
- Fail-open behavior for trace storage failures remains deliberate until there
  is a better recovery policy.

### Target shape

Split prompt building into three explicit phases:

1. `plan_prompt(...) -> PromptPlan`
   - pure;
   - no store writes;
   - contains typed components and render options.
2. `commit_prompt_plan(plan, store) -> StoredPrompt`
   - stores components and prompt object;
   - records derivations and refs;
   - returns object ids.
3. `render_model_input(stored_or_unstored_prompt, provider) -> ModelInput`
   - converts the prompt plan into provider-neutral model input;
   - provider adapter handles final wire format.

### Tests first

- Planning the same prompt twice produces equal plans without writing the store.
- Committing the same plan twice is idempotent at the object level.
- Rendering a stored and unstored equivalent plan produces the same
  provider-neutral `ModelInput`.
- Reconstructing a stored prompt verifies the payload hash.

### Implementation steps

1. Introduce `PromptPlan` and `StoredPrompt`.
2. Move component assembly into pure functions returning typed components.
3. Move store writes into `commit_prompt_plan()`.
4. Move model request construction into `render_model_input()`.
5. Update trace reconstruction to rebuild a `PromptPlan` before rendering.
6. Keep `PromptBuilder.build()` as a thin compatibility wrapper only while
   callers migrate.
7. Delete the compatibility wrapper once `ripple` shows no direct callers.

### Verification

- `uv run pytest tests/test_zeta_prompt.py tests/test_zeta_trace.py -q`
- `uv run pytest -q`
- `uvx --with radon radon cc src tests -s`

## 7. Resumable step engine

### Current read

The current agent loop is readable, but it mixes prompt assembly, model calls,
event emission, trace recording, tool execution, staged-effect policy,
telemetry, deadlines, and cancellation in one synchronous loop.

### Behavior to preserve

- `ask`, `propose`, and `do` workflow behavior remains unchanged.
- Max-turn, deadline, and cancellation semantics remain observable through
  current events.
- Staged effects still stop the run when configured to do so.
- Streaming answer text continues to work.
- Existing RPC and CLI callers keep receiving the same high-level result shape
  until the protocol replacement is explicit.

### Target shape

Represent the run as durable or serializable state plus atomic steps:

```text
RunState -> Step -> StepEffects -> RunState
```

Initial steps:

- `ReceiveUserMessage`
- `BuildPrompt`
- `CallModel`
- `RecordAssistant`
- `RecordCapabilityCall`
- `ExecuteCapability`
- `RecordCapabilityResult`
- `FinishRun`
- `AbortRun`

Effects are separate from committed state:

- live stream deltas;
- model status updates;
- event publications;
- store writes;
- capability invocations.

### Tool execution journal

Before any capability executes, record a pending call with stable identity.
After execution, record exactly one terminal result:

- completed;
- failed;
- cancelled;
- timed out;
- refused.

If a run resumes and sees a terminal result for the pending call, it reconciles
that result instead of invoking the capability again.

### Tests first

- A pure answer run executes the expected step sequence.
- A tool/capability run records pending call before invocation and terminal
  result after invocation.
- A crash/resume simulation with a terminal result does not invoke the
  capability twice.
- Cancellation between steps produces `AbortRun`.
- Deadline between steps produces `AbortRun`.

### Implementation steps

1. Add `RunState`, `Step`, `StepResult`, and `StepEffect` types.
2. Wrap the existing loop in a step runner without changing behavior.
3. Move prompt build/model call/tool execution into individual step functions.
4. Persist pending and terminal capability call lifecycle events.
5. Add reconciliation before invoking a pending capability.
6. Thread cancellation and deadline checks between steps.
7. Replace `run_agent_turn()` internals with the step runner.
8. Delete old loop helpers after caller and test coverage is accounted for.

### Verification

- `uv run pytest tests/test_zeta_agent.py tests/test_zeta_tools.py tests/test_zeta_trace.py -q`
- `uv run pytest -q`
- `uvx --with coverage coverage run -m pytest`
- `uvx --with coverage coverage report`
- `uvx --with radon radon cc src tests -s`

## 8. Replay, diff, and fork acceptance tests

### Current read

Prompt reconstruction exists, but replay is not yet the architectural acceptance
test. Zeta should be judged by whether stored runs can explain and reproduce
their own inputs.

### Behavior to preserve

- The trace store remains content-addressed and inspectable.
- Runtime events remain the ordering and causality source.
- Prompt objects remain reconstructible from linked components and derivations.

### Acceptance invariants

For every completed run in tests:

- Zeta can reconstruct every model input from stored objects.
- Re-rendering the reconstructed input verifies the stored hash.
- Every model output links to the prompt that produced it.
- Every capability result links to the call that produced it.
- Every runtime event that creates or consumes a durable value points at the
  relevant object id.
- A replay in deterministic-provider mode produces the same model outputs and
  capability lifecycle.

### Implementation steps

1. Add replay helpers that operate on the public trace/event APIs.
2. Add golden tests for pure answer, tool/capability, staged mutation, and
   aborted runs.
3. Add prompt diff helpers only after reconstruction is stable.
4. Add fork helpers only after replay can identify the exact branch point.
5. Keep user-facing replay commands out of scope until the library invariants
   are tested.

### Verification

- `uv run pytest tests/test_zeta_trace.py tests/test_zeta_agent.py -q`
- `uv run pytest -q`

## 9. Zeta core boundary

### Current read

`src/zeta` is intended to be product-neutral, while Sigil owns concrete local
tools, project context, CLI workflows, and policy. Some tests already assert
that `src/zeta` does not import `sigil`, and the capability plan keeps that
direction.

### Behavior to preserve

- Sigil can still register built-in capabilities.
- Zeta can still run a pure session with no Sigil-specific code.
- Existing `zeta` and `sigil zeta` CLI entrypoints continue to work until the
  CLI boundary is explicitly revised.

### Boundary rule

`src/zeta` owns:

- runtime records;
- prompt planning and tracing;
- provider-neutral model input/output;
- provider adapters;
- event/object storage contracts;
- capability protocol and mediation;
- RPC/session protocol.

Sigil owns:

- concrete built-in capabilities;
- workspace/project context policy;
- product CLI defaults;
- workflow names and user-facing command grouping;
- any integration with Sigil history/security state.

### Implementation steps

1. Keep the import-boundary test and expand it to cover new modules.
2. Move any newly discovered Sigil policy out of `src/zeta` before adding new
   abstractions around it.
3. Keep capability registration as the boundary for local tools.
4. Keep project-context loading injectable from RPC/CLI callers.
5. Document the boundary in existing docs only after the code shape stabilizes.

### Verification

- `uv run pytest tests/test_zeta_tools.py tests/test_zeta_agent.py -q`
- `uv run pytest -q`

## Cross-cutting risks

1. **Synchronous stdio blocks cancellation and timeouts.** Worker execution must
   be isolated in the RPC server, not the agent loop.
2. **Run id vs event id confusion.** Keep `run_id` separate from durable event
   ids unless Remi explicitly wants them unified.
3. **Schema drift.** Protocol docs and tests must assert the same shapes.
4. **Allowed tools vs registered tools.** Registration is not authorization.
5. **Capability ids vs aliases.** Registry identity is provider-qualified, but
   the model only sees per-run aliases. Keep alias resolution explicit and
   reject ambiguous runs.
6. **Model-facing metadata leak.** Trust and transport are runtime policy, not
   prompt text.
7. **Result normalization.** Display and trace code know several result shapes;
   normalize at the boundary without breaking summaries.
8. **Terminology churn.** Broad renames are allowed, but do them in one
   deliberate slice with tests instead of mixing vocabulary changes into every
   later behavior slice.
9. **Typed records without boundary discipline.** If dicts remain common inside
   the runtime, the migration adds ceremony without improving safety.
10. **Provider-neutral model objects that mirror one provider.** The model
   contract should describe Zeta's needs first and keep provider quirks in
   adapter metadata.
11. **Step engine overreach.** The first step engine should preserve current
   behavior and make resume/reconciliation possible; scheduling, workers, and
   distributed execution are separate decisions.
12. **Replay as a feature before replay as an invariant.** Library-level replay
   checks should pass before adding user-facing commands.
