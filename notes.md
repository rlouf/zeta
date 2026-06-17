# Sigil active execution plan

## Active order

1. Finish the Zeta durable timeline cleanup by removing the trace `run_event`
   chain.
2. Make the Zeta RPC syscall surface explicit and enforceable.
3. Make every tool a declared capability and mediate all model-to-tool calls
   through that capability interface.
4. Make model input/output provider-neutral inside Zeta.
5. Split prompt construction into plan, commit, and provider render phases.
6. Rework the agent loop into an explicit resumable step engine.
7. Promote replay/diff/fork invariants into acceptance tests.
8. Sharpen the `src/zeta` core boundary from Sigil integration.

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

## 4. Provider-neutral model input and output

### Second opinion status

- Tried `claude -p` as required for a complex refactor plan.
- The installed Claude CLI is not authenticated and returned
  `401 Invalid authentication credentials`.
- Tried `gemini -p`; Gemini is not installed in this environment. No usable
  external critique is folded in here.

### Current read

Zeta currently keeps Chat Completions-shaped messages internally and translates
Responses API payloads at the wire boundary. That means provider compatibility
concerns leak into prompt building, replay, and trace objects.

The current concrete boundaries are:

- `src/zeta/models/chat_completions.py` builds Chat Completions request bodies,
  streams SSE chunks into Chat-Completions-shaped payloads, and exposes
  `chat_completion_messages()`.
- `src/zeta/models/responses.py` takes Chat-Completions-shaped messages and
  tools, renders Responses request bodies, streams Responses events, then
  converts the result back into a Chat-Completions-shaped payload. It preserves
  raw Responses output items in `_responses_items` for replay.
- `src/zeta/agent.py` calls `chat_completion_messages()` through
  `request_assistant_message()`, then wraps the returned assistant-message dict
  in `AssistantMessage`.
- prompt tracing currently stores the assistant provider payload under
  `zeta.assistant_output.v1`.

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


### Slice 2: Chat Completions adapter - complete

Make Chat Completions the first explicit adapter:

- add `chat_completion_request_from_input(ModelInput)`;
- add `model_output_from_chat_completion(payload)`;
- keep `chat_completion_messages()` returning the current assistant dict until
  the agent migrates;
- keep `request_chat_completion()` accepting raw request bodies at its public
  boundary for now.

Behavior to preserve:

- streaming content and reasoning deltas are unchanged;
- fragmented tool calls are reassembled in the same order;
- usage-only chunks still populate telemetry;
- length-truncated tool calls still fail in the same caller path.

Tests first:

- golden Chat Completions request rendering from `ModelInput`;
- stream accumulator output converted to `ModelOutput`;
- `chat_completion_messages()` compatibility return stays unchanged.

Verification:

- `uv run pytest tests/test_zeta_model.py tests/test_zeta_agent.py -q` passed
  with 148 tests.
- `uv run pytest -q` passed with 815 tests and 4 skipped.
- `uv run coverage run -m pytest` and `uv run coverage report` passed with
  93% total coverage.

### Slice 3: Responses adapter - complete

Move the Responses bridge from "chat-shaped internal payloads" toward the same
`ModelInput` / `ModelOutput` contract:

- add `responses_request_from_input(ModelInput)`;
- add `model_output_from_responses_payload(payload)`;
- keep the existing `responses_request_body()` compatibility wrapper;
- keep `codex_completion_messages()` returning the current assistant dict until
  the agent migrates.

Behavior to preserve:

- system messages still become top-level `instructions`;
- tool messages still become `function_call_output`;
- assistant replay still prefers raw `_responses_items`;
- reasoning effort mapping remains unchanged;
- Responses usage maps onto the current token field names.

Tests first:

- golden Responses request rendering from `ModelInput`;
- replayed Responses items are passed through verbatim;
- Responses stream accumulator exposes `ModelOutput` with replay items;
- `codex_completion_messages()` compatibility return stays unchanged.

Verification:

- `uv run pytest tests/test_zeta_responses.py tests/test_zeta_agent.py -q` passed
  with 109 tests.
- `uv run pytest -q` passed with 816 tests and 4 skipped.
- `uv run coverage run -m pytest` and `uv run coverage report` passed with
  93% total coverage.

### Slice 4: agent model-call boundary - complete

Update the agent to request and carry `ModelOutput` internally:

- change `request_assistant_message()` to return `ModelOutput`;
- change `request_model_turn()` to build `AssistantMessage` from
  `ModelOutput.message`;
- keep `AssistantMessage.to_provider()` as the compatibility shape for
  prompt/timeline code until Section 6 renders `ModelInput` directly;
- continue storing current model telemetry fields.

Behavior to preserve:

- `run_agent_turn()` final text and streaming behavior are unchanged;
- model events keep the same JSON shape;
- tool calls still resolve through the capability projection;
- `request_assistant_message()` only changes its internal return type after
  all direct tests/callers are adjusted.

Tests first:

- pure answer run finalizes the same final text;
- reasoning content still appears in the model event;
- tool-call run still emits the same tool call/result events;
- model telemetry still attaches to the first tool result.

Verification:

- `uv run pytest tests/test_zeta_agent.py tests/test_zeta_model.py tests/test_zeta_responses.py -q`
  passed with 171 tests.
- `uv run pytest -q` passed with 818 tests and 4 skipped.
- `uv run coverage run -m pytest` and `uv run coverage report` passed with
  93% total coverage.

### Slice 5: provider-neutral trace payloads - complete

Change trace storage from provider-shaped assistant payloads to a
provider-neutral output plus adapter metadata.

Target trace data:

- model output message/content/tool calls;
- finish reason;
- usage;
- provider metadata;
- provider replay items, including Responses encrypted reasoning items.

Behavior to preserve:

- prompt reconstruction and replay keep passing;
- Responses replay items remain available when rendering the next request;
- existing trace CLI rendering keeps showing assistant content and tool calls;
- legacy trace objects degrade gracefully where possible.

Tests first:

- prompt trace stores provider-neutral output data;
- reconstructing a prompt with a Responses assistant still replays opaque items;
- trace CLI rendering still shows assistant/tool-call content;
- legacy assistant-message objects continue to render or fail open explicitly.

Verification:

- `uv run pytest tests/test_zeta_prompt.py tests/test_zeta_trace.py tests/test_zeta_responses.py -q`
  passed with 165 tests.
- `uv run pytest -q` passed with 820 tests and 4 skipped.
- `uv run coverage run -m pytest` and `uv run coverage report` passed with
  93% total coverage.

### Slice 6: compatibility cleanup - complete

Only after the adapter and agent boundaries use `ModelInput` / `ModelOutput`:

- run `ripple` on `chat_completion_messages()`,
  `codex_completion_messages()`, `responses_request_body()`,
  `responses_input_items()`, and assistant-message provider conversion helpers;
- remove compatibility helpers with no production callers;
- keep public shims where tests or protocol boundaries intentionally assert
  provider dicts.

Verification:

- `uv run ripple ...` could not run because `ripple` is not installed in this
  checkout.
- `rg` audit found production or protocol-boundary callers for
  `chat_completion_messages()`, `codex_completion_messages()`,
  `responses_request_body()`, `responses_input_items()`,
  `AssistantMessage.from_provider()`, and `AssistantMessage.to_provider()`, so
  no compatibility helper was removed in this slice.
- `uv run pytest tests/test_zeta_model.py tests/test_zeta_responses.py tests/test_zeta_agent.py tests/test_zeta_prompt.py -q`
  passed with 235 tests.
- `uv run pytest -q` passed with 820 tests and 4 skipped.
- `uvx --with radon radon cc src tests -s` passed.

## 5. Prompt plan, commit, and render

### Current read

`PromptBuilder.build()` both assembles prompt components and stores trace
objects when a store is available. That makes it harder to dry-run prompt
construction, diff prompt inputs, compare provider renders, or replay a prompt
without side effects.

### Second opinion status

- Tried `claude -p` as required for a complex refactor plan.
- The installed Claude CLI is not authenticated and returned
  `401 Invalid authentication credentials`.
- Tried `gemini -p`; Gemini is not installed in this checkout.

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

### Slice 1: plan, commit, and render API - complete

Introduced `PromptPlan`, `StoredPrompt`, `plan_prompt()`,
`commit_prompt_plan()`, and `render_model_input()`.

Behavior preserved:

- `PromptBuilder.build()` remains the compatibility wrapper for existing
  callers.
- Prompt component order, prompt object hashes, derivations, and refs keep their
  previous behavior.
- Fail-open trace storage still returns an unstored prompt shape.

Verification:

- `uv run pytest tests/test_zeta_prompt.py tests/test_zeta_trace.py -q` passed
  with 147 tests.
- `uv run pytest -q` passed with 823 tests and 4 skipped.
- `uv run coverage run -m pytest` and `uv run coverage report` passed with
  93% total coverage.
- `uvx --with radon radon cc src/zeta/prompt src/zeta/agent.py tests/test_zeta_prompt.py -s`
  passed.

### Slice 2: reconstruction renders through plans - complete

Updated stored prompt reconstruction to rebuild a `PromptPlan` from the prompt
component closure before rendering provider-neutral `ModelInput`.

Behavior preserved:

- Existing `ReconstructedPrompt` accessors keep working for current callers.
- Payload verification still compares the stored prompt hash against the
  rendered Chat Completions request body.
- Legacy no-thinking prompts still reconstruct with `thinking=None`.

Verification:

- `uv run ripple src/zeta/prompt/builder.py reconstructed_prompt_request`
  could not run because `ripple` is not installed in this checkout.
- `uv run pytest tests/test_zeta_prompt.py tests/test_zeta_trace.py -q` passed
  with 147 tests.
- `uv run pytest -q` passed with 823 tests and 4 skipped.
- `uv run ruff check src tests` passed.
- `uvx --with radon radon cc src/zeta/prompt src/zeta/agent.py tests/test_zeta_prompt.py -s`
  passed.

### Slice 3: agent caller migration and wrapper removal - complete

Migrated the agent model-call path from `PromptBuilder.build()` to the explicit
`plan_prompt()` -> `commit_prompt_plan()` -> `render_model_input()` phases.
After migrating the remaining test setup callers, removed the compatibility
wrapper.

Behavior preserved:

- `request_model_turn()` still records prompt traces and assistant traces from
  the committed prompt graph.
- Model calls still receive the same messages, tool descriptors, and tool
  choice through provider-neutral `ModelInput`.
- Prompt tests use the same explicit phases as production code.

Verification:

- `uv run ripple src/zeta/prompt/builder.py PromptBuilder.build` could not run
  because `ripple` is not installed in this checkout.
- `rg -n "\\.build\\(" src tests` found no direct callers.
- `uv run pytest tests/test_zeta_agent.py tests/test_zeta_prompt.py tests/test_zeta_trace.py -q`
  passed with 237 tests.
- `uv run pytest -q` passed with 823 tests and 4 skipped.
- `uv run coverage run -m pytest` and `uv run coverage report` passed with
  93% total coverage.
- `uvx --with radon radon cc src tests -s` passed.
- `uv run pre-commit run --all` passed.

## 6. Resumable step engine

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

### Slice 1: in-memory step records - complete

Added the first explicit step-engine surface without changing execution
semantics:

- `RunState`;
- `Step`;
- `StepResult`;
- `StepEffect`.

The existing loop now records an in-memory step sequence around the current
budget check, prompt build, model call, assistant recording, capability-call
block, and finish points. Durable resume, pending capability journals, and
reconciliation remain future slices.

Behavior preserved:

- Pure answer runs still emit the same model event and final text.
- Tool and trace behavior remains under the existing helper functions.
- `AgentTurnState` remains as a compatibility alias for current tests and
  call sites.

Verification:

- `claude -p ...` could not provide a second opinion because the installed CLI
  returned `401 Invalid authentication credentials`.
- `gemini -p ...` could not provide a second opinion because `gemini` is not
  installed in this checkout.
- `uv run ripple src/zeta/agent.py run_agent_turn` and
  `uv run ripple src/zeta/agent.py request_model_turn` could not run because
  `ripple` is not installed in this checkout.
- `uv run pytest tests/test_zeta_agent.py tests/test_zeta_tools.py tests/test_zeta_trace.py -q`
  passed with 247 tests and 2 skipped.
- `uv run pytest -q` passed with 823 tests and 4 skipped.
- `uv run coverage run -m pytest` and `uv run coverage report` passed with
  93% total coverage.
- `uvx --with radon radon cc src/zeta/agent.py tests/test_zeta_agent.py -s`
  passed.
- `uv run pre-commit run --all` passed.

### Slice 2: extracted step runner boundary - complete

Moved the existing model/tool loop body behind `run_agent_steps()` while keeping
the same step sequence and runtime behavior. This gives later slices a stable
place to replace coarse helper calls with smaller step functions.

Behavior preserved:

- `run_agent_turn()` still owns setup: endpoint check, deadline normalization,
  capability projection, prompt builder creation, and initial `RunState`.
- `run_agent_steps()` owns turn iteration, model-turn execution, assistant event
  recording, capability execution, and terminal result construction.
- Tool runs now have an asserted in-memory sequence for budget check, prompt
  build, model call, assistant recording, capability call, capability execution,
  capability result, and finish.

Verification:

- `uv run pytest tests/test_zeta_agent.py tests/test_zeta_tools.py tests/test_zeta_trace.py -q`
  passed with 247 tests and 2 skipped.
- `uv run pytest -q` passed with 823 tests and 4 skipped.
- `uv run coverage run -m pytest` and `uv run coverage report` passed with
  93% total coverage.
- `uv run ty check` passed.
- `uvx --with radon radon cc src/zeta/agent.py tests/test_zeta_agent.py -s`
  passed.
- `uv run pre-commit run --all` passed.

### Slice 3: prompt build step function - complete

Split prompt build/render out of `request_model_turn()` into
`build_prompt_step()`, returning a committed `PreparedPrompt` plus the
provider-neutral `ModelInput`.

Behavior preserved:

- `request_model_turn()` still records the same step sequence:
  `build_prompt`, `call_model`, `record_assistant`.
- The build step still commits the prompt graph before the model request.
- The model call still receives rendered provider-neutral input.

Verification:

- `uv run ripple src/zeta/agent.py request_model_turn` could not run because
  `ripple` is not installed in this checkout.
- `uv run pytest tests/test_zeta_agent.py tests/test_zeta_tools.py tests/test_zeta_trace.py -q`
  passed with 248 tests and 2 skipped.
- `uv run pytest -q` passed with 824 tests and 4 skipped.
- `uv run coverage run -m pytest` and `uv run coverage report` passed with
  93% total coverage.
- `uv run ty check` passed.
- `uvx --with radon radon cc src/zeta/agent.py tests/test_zeta_agent.py -s`
  passed.
- `uv run pre-commit run --all` passed.

### Slice 4: model call and assistant recording steps - complete

Split the remaining pieces of `request_model_turn()` into explicit step
functions:

- `call_model_step()`;
- `record_assistant_step()`.

Behavior preserved:

- Model call status handling and streaming still flow through
  `request_assistant_message()`.
- Assistant trace recording still stores provider-neutral model output linked to
  the committed prompt.
- Model telemetry still updates `RunState` exactly once per model call.

Verification:

- `uv run ripple src/zeta/agent.py request_model_turn` could not run because
  `ripple` is not installed in this checkout.
- `uv run pytest tests/test_zeta_agent.py tests/test_zeta_tools.py tests/test_zeta_trace.py -q`
  passed with 250 tests and 2 skipped.
- `uv run pytest -q` passed with 827 tests and 4 skipped.
- `uv run coverage run -m pytest` and `uv run coverage report` passed with
  93% total coverage.
- `uv run coverage run -m pytest` and `uv run coverage report` passed with
  93% total coverage.
- `uv run ty check` passed.
- `uvx --with radon radon cc src/zeta/agent.py tests/test_zeta_agent.py -s`
  passed.
- `uv run pre-commit run --all` passed.

### Slice 5: capability execution step function - complete

Extracted the per-tool-call body of `run_capability_calls()` into
`run_capability_step()`. This is still the existing behavior under the hood, but
the step runner now has a single boundary for:

- budget check before a capability call;
- capability call recording;
- capability execution;
- capability result recording.

Behavior preserved:

- Tool-call events still stream before capability execution.
- Tool-result events still attach trace and telemetry through the existing
  helper path.
- Staged-effect and stop behavior still live in `run_capability_calls()`.

Verification:

- `uv run ripple src/zeta/agent.py run_capability_calls` could not run because
  `ripple` is not installed in this checkout.
- `uv run pytest tests/test_zeta_agent.py tests/test_zeta_tools.py tests/test_zeta_trace.py -q`
  passed with 251 tests and 2 skipped.
- `uv run pytest -q` passed with 826 tests and 4 skipped.
- `uv run ty check src tests` passed. A broader `uv run ty check` also scans
  the untracked `.worktrees/` checkout and fails on that separate tree, so the
  scoped project command is the relevant verification.
- `uvx --with radon radon cc src/zeta/agent.py tests/test_zeta_agent.py -s`
  passed.
- `uv run pre-commit run --all` passed.

### Slice 6: capability lifecycle status fields - complete

Made the existing capability event pair explicit as a lifecycle journal without
adding new event types:

- `tool_call` events now carry `status="pending"`;
- `tool_result` events now carry terminal status:
  - `completed` for successful capability results;
  - `refused` for validation or policy refusals;
  - `failed` for execution failures.

Behavior preserved:

- There is still exactly one `tool_call` event before execution and one
  `tool_result` event after each capability outcome.
- Existing durable event projection continues to use the same event types.
- Trace object links for calls and results are unchanged.

Verification:

- `uv run ripple src/zeta/agent.py ToolCallRuntimeEvent.to_event` and
  `uv run ripple src/zeta/agent.py ToolResultRuntimeEvent.to_event` could not
  run because `ripple` is not installed in this checkout.
- `uv run pytest tests/test_zeta_agent.py tests/test_zeta_tools.py tests/test_zeta_trace.py -q`
  passed with 251 tests and 2 skipped.
- `uv run pytest -q` passed with 827 tests and 4 skipped.
- `uv run coverage run -m pytest` and `uv run coverage report` passed with
  93% total coverage.
- `uv run ty check src tests` passed.
- `uvx --with radon radon cc src/zeta/agent.py tests/test_zeta_agent.py -s`
  passed.
- `uv run pre-commit run --all` passed.

### Slice 7: terminal capability result reconciliation - complete

Added the first resume-facing reconciliation check: before invoking a capability
call, `run_capability_step()` now looks for an existing terminal `tool_result`
with the same `tool_call_id` in `RunState.events`. If one exists, the step
records `record_capability_result` and does not invoke the capability again.

Behavior preserved:

- Fresh capability calls still emit the same `tool_call` and `tool_result`
  events.
- Existing terminal results are not duplicated into `RunState.events`.
- The reconciliation path is in-memory only; rebuilding `RunState` from durable
  events remains a later slice.

Verification:

- `uv run ripple src/zeta/agent.py run_capability_step` could not run because
  `ripple` is not installed in this checkout.
- `uv run pytest tests/test_zeta_agent.py tests/test_zeta_tools.py tests/test_zeta_trace.py -q`
  passed with 252 tests and 2 skipped.
- `uv run pytest -q` passed with 828 tests and 4 skipped.
- `uv run coverage run -m pytest` and `uv run coverage report` passed with
  93% total coverage.
- `uv run ty check src tests` passed.
- `uvx --with radon radon cc src/zeta/agent.py tests/test_zeta_agent.py -s`
  passed.
- `uv run pre-commit run --all` passed.

## 7. Replay, diff, and fork acceptance tests

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

## 8. Zeta core boundary

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
