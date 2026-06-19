# Refactor Boundary Sketch

Thought experiment: if Zeta/Sigil were rewritten from scratch, the main
boundary correction would be to stop using "session" as the meeting point for
resource construction, turn orchestration, event dispatch, event recording, and
trace projection.

## Core Principle

Separate concepts by runtime ownership:

- Kernel primitives are pure data.
- Runtime executes turns using explicit ports.
- Sessions bind resources to a named scope.
- Dispatch routes already-accepted events.
- Trace projects durable history into object/read models.
- Sigil is a host UX over Zeta.

The test for a boundary is: "what kind of change should make this file change?"
For example, changing SQLite schema should not change turn orchestration.
Changing prompt compaction should not change session construction. Adding a new
RPC method should not change kernel event definitions unless it needs a new
domain concept.

## Kernel

Location:

```text
zeta/kernel/
  events.py
  objects.py
  capabilities.py
  model.py
  turns.py
```

Owns:

- `Event`, `DraftEvent`, `EventFilter`
- `Object`, `Derivation`, `Ref`
- `Capability`, `CapabilitySpec`, `CapabilityPolicy`, `CapabilityResult`
- `ModelInput`, `ModelOutput`, `ModelUsage`
- `TurnRequest`, `TurnResult`, `TurnOutcome`

Rule: kernel modules contain dataclasses, literals/enums, and pure conversion
functions. They do not know about SQLite, RPC, the filesystem, network
transports, or the CLI.

Kernel should answer questions like:

- What is an event?
- What is a turn request?
- What does a capability declare?
- What is the shape of model input and output?
- What is the difference between a draft event and a durable event?

Kernel should not answer questions like:

- Where are events stored?
- Which model endpoint is active?
- Which session directory is used?
- Which agents should react to an event?
- How should a terminal render progress?

This layer should be boring and stable. Most objects should be immutable.
Validation here should protect domain invariants, not transport-specific
schemas. For instance, an event timestamp or idempotency key shape can live
here; JSON-RPC error formatting should not.

## Ports

Location:

```text
zeta/ports/
  event_log.py
  object_store.py
  model_gateway.py
  capability_registry.py
  clock.py
```

Owns explicit interfaces for external dependencies:

```python
class EventLog:
    def append(self, draft: DraftEvent) -> Event: ...
    def list(self, filter: EventFilter) -> list[Event]: ...


class ObjectStore:
    def put(self, obj: Object) -> ObjectId: ...
    def get(self, id: ObjectId) -> Object | None: ...


class ModelGateway:
    async def generate(
        self,
        input: ModelInput,
        config: ModelConfig,
    ) -> ModelOutput: ...
```

Rule: orchestration code depends on ports, not concrete SQLite stores, concrete
registries, or model transport modules.

Ports describe what runtime needs from the outside world. They should be
minimal, capability-shaped, and easy to fake in tests.

Useful ports:

- `EventLog`: append durable events and list slices.
- `ObjectStore`: write/read content-addressed objects and derivations.
- `CapabilityRegistry`: resolve model-visible tool names to executable
  capabilities.
- `CapabilityExecutor`: run or stage one capability.
- `ModelGateway`: call the selected model transport.
- `Clock`: provide timestamps and deadlines without hard-coding wall time.
- `Publisher`: notify subscribers after an event is accepted.

Ports should avoid policy. For example, an `EventLog` should not decide whether
an event triggers an agent. A `ModelGateway` should not decide whether a tool
call may run directly. A `CapabilityRegistry` may validate arguments, but the
runtime decides execution mode.

The practical payoff is that runtime tests can use in-memory fakes without
pulling in SQLite, filesystem state, or real model transport behavior.

## Runtime

Location:

```text
zeta/runtime/
  runner.py
  context.py
  tool_calls.py
  model_turns.py
  cancellation.py
```

Owns:

- `TurnRunner`
- `TurnContext`
- tool-call parsing, validation, and execution
- model call loop
- cancellation/deadline behavior
- runtime event emission

Main API:

```python
result = await TurnRunner(deps).run(TurnRequest(...))
```

Rule: runtime does not know about RPC request names such as
`session.turn.requested`. It runs turns.

Runtime is the agent loop boundary. It takes a validated `TurnRequest`, a
timeline, runtime dependencies, and execution policy; it returns a `TurnResult`.

Runtime owns sequencing:

1. Load the prior timeline.
2. Build and commit a prompt.
3. Call the model.
4. Parse assistant output and tool calls.
5. Validate capability calls.
6. Execute or stage capabilities.
7. Emit runtime events.
8. Stop on final text, staged effect, budget, deadline, or cancellation.

Runtime should emit events through a narrow recorder/publisher interface. It
should not know whether events are being displayed in a terminal, streamed over
RPC, written to SQLite, or routed to agents.

Runtime is also the right place for turn-local concepts:

- `TurnContext`
- `RunState`
- `ModelTurn`
- `AssistantMessage`
- `ModelToolCall`
- `ToolCallValidation`
- `CapabilityCallInvocation`
- cancellation/deadline checks
- step accounting

Runtime should not construct sessions, open stores, parse JSON-RPC params, or
produce UI-specific summaries.

## Sessions

Location:

```text
zeta/sessions/
  session.py
  factory.py
  state_dir.py
```

Owns:

- `Session`
- `SessionFactory`
- state directory conventions
- binding concrete stores, registries, and model gateways into runtime deps

Rule: a session is a named resource scope. It can create a `TurnRunner`, but it
is not itself the runner.

```python
session = SessionFactory.default().open("abc")
runner = session.turn_runner()
```

A session is a handle to durable resources under a stable identity. It answers:

- What is the session id?
- Which event log belongs to it?
- Which object store scope belongs to it?
- Which capability registry is available?
- Which state/session directories are used?
- Which model gateway is configured by default?

Session creation may touch the filesystem and concrete adapters. That is the
point of the boundary: it is where abstract runtime ports become concrete local
resources.

Session should not:

- parse a `session.run` RPC payload
- decide whether a workflow is `ask`, `propose`, or `do`
- execute a model/tool loop
- route event-triggered agents
- summarize trace ids for a response payload

If a host wants to run a turn, it should ask the session for dependencies and
construct a runtime runner. If a host wants event-driven execution, dispatch
should call a handler that uses the session to construct the runner.

## Event Bus And Dispatch

Location:

```text
zeta/dispatch/
  bus.py
  dispatcher.py
  subscriptions.py
  agents.py
```

Dispatch is not persistence. It is routing after persistence.

```text
producer -> EventLog.append(draft) -> Event
         -> EventBus.publish(event)
         -> Dispatcher subscribers may react
```

For request events:

```text
session.turn.requested
  -> dispatcher routes to SessionTurnHandler
  -> handler calls TurnRunner
```

Direct turn execution should still be possible without dispatch:

```python
await TurnRunner(...).run(request)
```

This keeps dispatch optional instead of foundational.

The append path and the routing path should be separate:

- `EventLog.append(draft)` normalizes and persists facts.
- `EventBus.publish(event)` notifies listeners about accepted facts.
- `Dispatcher` subscribes to events and starts follow-up work.

Dispatch owns:

- event subscriptions
- trigger matching
- work-event lifecycle, such as pending/running/completed/failed
- event-triggered agent attempts
- fanout to in-process subscribers

Dispatch does not own:

- the durable shape of events
- idempotency semantics beyond respecting append results
- core turn execution
- prompt construction
- object trace projection

The important failure mode to avoid is accidental replay side effects. Reading
or importing old events must not silently re-run agents. That means dispatch
should react only to events that were explicitly published as newly accepted,
not to every event returned by a query.

This boundary also makes high-volume events easier to reason about. Model stream
chunks and status updates may be persisted or published to UI subscribers
without necessarily going through agent trigger matching.

## Prompt And Context Assembly

Location:

```text
zeta/context/
  components.py
  builder.py
  transforms.py
  compaction/
  instructions.py
  skills.py
```

Owns:

- prompt components
- trace object creation for prompts
- compaction
- system/project instructions
- skill prompt injection

Interface:

```python
plan = prompt_builder.plan(request, timeline, capabilities)
stored = prompt_builder.commit(plan)
model_input = prompt_builder.render(stored)
```

Context assembly owns model-facing context, not turn control flow.

It should decide:

- which timeline events become chat messages
- how system/project instructions are represented
- where tool descriptors appear
- how skills are discovered and injected
- how prompt components are traced as objects
- how compaction transforms reduce prompt size
- how context usage is measured

It should not decide:

- whether a tool call may execute directly
- whether the turn should stop after a staged effect
- how events are stored
- how RPC response payloads are formatted
- how terminal progress is rendered

The prompt builder should have a pure planning phase and a side-effecting commit
phase. Planning should be easy to test without a store. Commit should be the
only step that writes prompt/component objects and derivations.

This keeps prompt bugs localized. A change to compaction should affect prompt
tests and trace object expectations, not session construction or event dispatch.

## Trace Projection

Location:

```text
zeta/trace/
  projection.py
  replay.py
  query.py
  render.py
```

Owns:

- reconstructing prompt/tool/model object graphs from durable events
- trace summaries
- replay/query/render helpers

Rule: runtime may write events and objects, but trace projection is a read model
over durable events and stored objects. It should not be mixed into session
construction or turn orchestration except through a small optional hook.

Trace projection answers: "given durable events and stored objects, what graph
or summary can we reconstruct?"

It owns:

- mapping model events to prompt object ids
- mapping assistant responses to message object ids
- mapping tool call/result events to tool object ids
- rebuilding trace closures
- replaying a turn or session timeline
- rendering/querying trace data for humans and tests

It should tolerate partial data. Missing trace objects should degrade into a
diagnostic or empty projection, not break turn execution. Projection failures
are observability failures unless the caller explicitly requested strict replay.

Runtime may opportunistically call projection after appending a durable event to
keep stores warm, but the projection code should remain callable later from the
event log alone. That keeps trace repair and replay possible.

## Adapters

Location:

```text
zeta/adapters/
  sqlite_event_log.py
  sqlite_object_store.py
  memory_event_log.py
  memory_object_store.py
  openai_chat_completions.py
  codex_responses.py
  rpc.py
```

Rule: adapters implement ports. They do not own domain policy.

Adapters are the concrete edge of the system:

- SQLite event log
- SQLite object store
- in-memory stores for tests
- chat-completions transport
- Codex responses transport
- JSON-RPC transport
- filesystem-backed config/profile loading

Adapters may translate between external schemas and kernel/runtime objects.
They should keep that translation close to the external dependency. For example,
OpenAI chat-completions response parsing belongs near the chat-completions
adapter; the runtime should receive a normalized `ModelOutput`.

Adapters should not import host UI code. They should also avoid reaching upward
into runtime policy. A model adapter can expose provider metadata; it should not
decide whether a returned tool call is allowed.

The expected test shape is:

- adapter tests cover schema translation and persistence behavior
- runtime tests use fake ports
- integration tests wire real adapters together

## Host Apps

`sigil` is one host app using the Zeta runtime.

```text
sigil/
  cli/
  workflows/
  display/
  tools/
  shell/
  status.py
```

Sigil owns:

- CLI command UX
- shell bindings
- terminal display
- workflow names: `ask`, `propose`, `do`
- local built-in tools
- turn history UX

Sigil should not own generic agent runtime behavior.

Sigil translates human workflows into runtime requests. It can choose defaults,
name workflows, register local tools, and render output.

Sigil owns the UX contract:

- shell integration
- command naming and flags
- `ask`, `propose`, and `do` semantics from the user's point of view
- terminal progress and Markdown streaming
- built-in local tools and their review/staging behavior
- status reporting
- turn history presentation

Sigil should delegate generic mechanics to Zeta:

- event log semantics
- content-addressed object storage
- model/tool loop execution
- capability validation and invocation protocol
- prompt component tracing
- trace projection

This keeps Zeta usable by other hosts. A daemon, RPC server, test harness, or
future GUI should not have to depend on Sigil's shell/display assumptions to run
an agent turn.

## Current `session.py` Split

The current `src/zeta/session.py` pieces would move roughly like this:

```text
Session
  zeta/sessions/session.py

default_session, session_for_id, zeta_state_dir
  zeta/sessions/factory.py

SessionRunParams, SessionRequestError
  zeta/runtime/requests.py
  or zeta/sessions/requests.py if only RPC-session scoped

run_session_turn
  zeta/runtime/session_turn_handler.py
  possibly renamed handle_session_turn_request

run_session_turn_from_event
  zeta/dispatch/session_handlers.py

session_event_dispatcher
  zeta/dispatch/session_dispatch.py

record_user_message, record_runtime_draft, live_runtime_event
  zeta/runtime/event_recorder.py

project_trace_for_turn, session_trace_result
  zeta/trace/projection.py

session_result
  zeta/runtime/results.py
```

## Boundary To Enforce

Do not let "session" become the place where orchestration, event recording,
dispatch, and trace summaries meet.

Session should be a resource boundary. Turn execution should be a runtime
boundary. Dispatch should be a routing boundary. Trace should be a projection
boundary.

When in doubt, ask which object should be able to exist without the others:

- A `Session` should exist without running a turn.
- A `TurnRunner` should run against fake ports without a concrete session.
- A dispatcher should route an accepted event without knowing SQLite details.
- A trace projector should rebuild summaries without triggering agents.
- Sigil should provide UX without owning the runtime loop.
