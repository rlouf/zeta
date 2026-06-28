# `RunState` / `RunInfo` Step Boundary

The core loop should expose one atomic transition:

```python
state, info = await step(state, deps)
```

`step` advances the run by one model call or one tool batch. It does not persist
events, publish events, or update host/session state directly. It returns enough
information for the host layer to do those things.

## Shape

```python
@dataclass(frozen=True)
class RunState:
    objective: str
    timeline: tuple[TimelineEvent, ...]
    events: tuple[DraftEvent, ...] = ()
    pending_tool_calls: tuple[dict[str, Any], ...] = ()
    next_model_caused_by: str | None = None
    turn: int = 0
    stop: StopReason | None = None
```

`RunState` is the resumable loop state. It should contain the durable facts
needed to continue the run, not host plumbing.

```python
@dataclass(frozen=True)
class RunInfo:
    kind: Literal["model", "tools", "stopped", "aborted"]
    appended_events: tuple[DraftEvent, ...] = ()
    prompt_trace: PromptTrace | None = None
    model_telemetry: dict[str, Any] = field(default_factory=dict)
    staged_effect: dict[str, Any] | None = None
    final_answer: str = ""
    answer_streamed: bool = False
```

`RunInfo` is the delta from one step. It should contain what the host needs to
persist, publish, trace, and report.

## Control Flow

```python
async def step(
    state: RunState,
    deps: RunDependencies,
) -> tuple[RunState, RunInfo]:
    if state.stop is not None:
        return state, RunInfo(kind="stopped")

    check_abort_or_budget(state, deps)

    if state.pending_tool_calls:
        return await step_tools(state, deps)

    return await step_model(state, deps)
```

`step_model` builds the prompt, calls the model, converts the model output into a
draft model event, and returns updated state plus `RunInfo(kind="model")`.

`step_tools` executes the pending batch, converts results into draft tool events,
clears `pending_tool_calls`, and returns `RunInfo(kind="tools")`.

## Host Responsibility

The host/runtime wrapper owns side effects:

```python
while state.stop is None:
    state, info = await step(state, deps)

    for draft in info.appended_events:
        persisted = record_runtime_event(draft, runtime_context, run_id)
        publish_event(persisted)

    update_trace_projection(info, runtime_context.trace_store)
```

This keeps the reasoner testable and makes persistence a caller concern.

## Tradeoff

This introduces explicit state and info types, but it removes hidden mutation
from the loop. The durable event log remains the source of truth; `RunState` is
only the in-memory/resumable loop cursor, and `RunInfo` is the per-step delta.
