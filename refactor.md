# Projection Function Naming Refactor

## Goal

Use one repo-wide naming convention for functions that derive a read-side object
from events, drafts, records, payloads, or prompt components.

The convention:

- `project_<plural_target>` folds many source items into projected target
  objects.
- `project_one_<singular_target>` projects one source item and may return
  `None`.
- The name after `project` is the target being produced, not the source being
  read.
- Do not use `project_` for loading a project directory or for provider payload
  conversion. Reserve it for read-model/projection behavior.
- Keep type and domain noun refactors separate from this convention. This pass
  decides function names, not whether the target type should be renamed.

## Rules

Apply these rules before renaming anything:

- Do not create a read model just because event payload access is annoying.
  Create one only when multiple callers need the same normalized view, or when
  the normalization itself is meaningful domain logic.
- Prefer carrying the source event until a separate object proves its value. If
  the event already contains the fields and callers only need one or two of
  them, direct access is clearer than a projection layer.
- Keep helpers for policy, not for field lookup. Stable event keys, idempotency
  contracts, lifecycle status policies, external provider normalization, and
  emitted event schemas can earn helpers. `payload.get("field")` usually cannot.
- Delete single-use projection helpers instead of renaming them. A bad name may
  be evidence that the abstraction should not exist.
- Distinguish write-side schema helpers from read-side projections. Helpers
  that construct lifecycle event payloads can be useful; helpers that read the
  same schema back need stronger justification.
- If a helper remains, name the policy or schema it encodes. If the best name is
  still just the field being read, inline it.
- Tests should pin behavior, not convenience objects. Prefer tests for retry
  numbering, terminal behavior, emitted events, idempotency, and provider
  normalization over tests that only prove a projection object mirrors payload
  fields.
- Let duplication earn abstraction. One caller doing a direct payload read is
  fine; repeated validation logic can justify a helper once the duplication is
  real.

Example:

```python
def project_queue_items(events: Iterable[Event]) -> list[QueueItem]:
    items: dict[str, QueueItem] = {}
    for event in events:
        item = project_one_queue_item(event)
        if item is not None:
            items[item.queue_item_id] = item
    return list(items.values())


def project_one_queue_item(event: Event) -> QueueItem | None:
    ...
```

## Apply This Convention

### Records Provenance

File: `src/zeta/records/provenance.py`

Current names:

- `project_trace_events`
- `project_trace_drafts`
- `project_model_event`
- `project_tool_call_event`
- `project_tool_result_event`

Target direction:

- Rename the many-item functions so the target is explicit once the target noun
  is chosen. Do not name the function after the source events.
- Rename single-event functions to follow the convention:
  `project_one_trace_model_event`, `project_one_trace_tool_call`, and
  `project_one_trace_tool_result`, or keep them private if only the top-level
  projector calls them.
- Avoid mixing source nouns and target nouns in the public function names.

The target type decision is tracked under "Separate Noun Refactors."

- `project_model_event`, `project_tool_call_event`, and
  `project_tool_result_event` are single-event helpers used by the top-level
  projector. Keep them private unless callers need to project one event
  directly.
- `model_trace_data`, `tool_call_object_data`, `tool_result_object_data`, and
  `tool_event_derivation_params` encode object-store schemas. They are not just
  field lookups; keep them if the schema centralization remains useful.

### Records Timeline

File: `src/zeta/records/timeline.py`

Current names:

- `history_event_record`
- `effect_event_record`
- `event_from_effect_record`
- `event_from_record`
- `turn_record`
- `effect_record`
- `TURN_EVENT_COMPLETED`
- `TURN_EVENT_FAILED`
- `TURN_RECORD_SCHEMA`
- `turn_event_type`

Target direction:

- Eliminate `src/zeta/records/timeline.py` from Zeta. It currently mixes two
  concerns that should move elsewhere.
- Move Zeta run lifecycle event vocabulary out of this module and into
  `zeta/run/`. The current `zeta.turn.completed` / `zeta.turn.failed` names
  still carry the old noun after the turn-to-run rename.
- Rename the lifecycle events around the current domain noun, for example
  `zeta.run.completed` / `zeta.run.failed`, unless the runtime package settles
  on a narrower prefix.
- Move run lifecycle draft/event constructors near the run domain. Sigil
  history can project those events, but it should not own their canonical event
  names.
- Rename event-to-record projectors:
  `history_event_record` should become `project_one_timeline_record` or a more
  specific target like `project_one_turn_record`.
- Rename `effect_event_record` to `project_one_effect_record`.

The `HistoryView` noun decision is tracked under "Separate Noun Refactors."

- Move the Sigil-facing history read model out of Zeta after the run lifecycle
  event vocabulary is owned by `zeta/run/`. The target should be Sigil history,
  not a renamed Zeta records module.
- `turns_by_id` and `effects_by_id` are many-event projections. Rename them
  toward the read model they produce, for example `project_turn_records_by_id`
  and `project_effect_records_by_id`.
- `history_event_record` and `effect_event_record` are single-item projections.
  Rename them only if the projected record is still useful; otherwise inline
  the direct event/record conversion at the caller.
- `event_time`, `turn_sort_key`, `effect_sort_key`, and `optional_match` are
  small local helpers. Inline them if they remain single-use and do not name a
  policy.

### Project Directory Loading Conflict

File: `src/zeta/process.py`

Current name:

- `default_session`
- `session_for_id`

Target direction:

- `process.py` is also awkward as the home for runtime-context construction.
  The functions that assemble event sinks, trace stores, tool registries, and
  directories should move with `RuntimeContext` once `SessionScope` is renamed.

### Records Event Helpers

File: `src/zeta/records/events.py`

Current direction:

- Keep idempotency helpers (`event_idempotency_key`,
  `durable_event_idempotency_key`) when they encode acceptance/dedupe policy.
  Reconcile duplicate policy with Sigil's `durable_idempotency_key`.
- Keep the `*_draft` suffix for functions that construct a `DraftEvent` and
  centralize an emitted event schema.
- Current schema constructors:
  `draft_from_runtime_event`, `draft_from_boundary_event`, `model_call_draft`,
  `tool_call_draft`, `turn_aborted_draft`, `stream_chunk_draft`,
  `status_update_draft`, `user_message_draft`, and `durable_event_draft`.
- Move `turn_aborted_draft` with the run lifecycle event vocabulary. It still
  emits `zeta.turn.failed` after the turn-to-run rename.
- Inline thin forwarding constructors if they only choose an event type and add
  no schema, idempotency, or lifecycle policy.
- Use `*_draft` when the function returns a `DraftEvent`.
- Use `draft_from_*` when converting an existing source representation into a
  `DraftEvent`. `draft_from_runtime_event` and
  `draft_from_boundary_event` follow this rule.
- Keep semantic constructors as `*_draft` when they build a named durable draft
  from explicit arguments, for example `model_call_draft`,
  `tool_call_draft`, `status_update_draft`, and `user_message_draft`.
- Use `event_from_*` only for reconstruction from an existing serialized or
  persisted representation into an actual `Event`. Examples:
  `event_from_record`, `event_from_effect_record`, and `event_from_row`.
- Do not use `to_*_event` for semantic event construction. Runtime/domain code
  should emit through `DraftEvent`.
- Use `*_event_payload` for dict builders that shape the payload for a future
  event draft. Do not use plain `*_event` for these functions because durable
  facts should pass through `DraftEvent` intentionally.
- `durable_view_type`, `event_timeline_type`, and `draft_timeline_type` share
  one timeline-type decoder in `records/events.py`.
- `durable_model_event_payload`, `durable_tool_event_payload`, and
  `durable_payload` are schema-normalization helpers. Keep them if they remain
  the single place that strips non-durable fields.
- `tool_result_status`, `normalized_tool_result`, and `tool_failure_message`
  encode tool-result policy. Keep them, but keep their names policy-oriented.

### Draft Dispatch Boundary

Files:

- `src/zeta/orchestration/dispatch.py`
- `src/zeta/rpc/routes.py`
- `src/zeta/run/thread_run.py`
- `src/sigil/agent_io.py`

Current direction:

- `*_draft` constructors should not send to dispatch. They should return
  `DraftEvent` values only.
- Dispatch happens when a caller passes a draft to
  `EventDispatcher.publish_event` or `EventDispatcher.publish_and_run`.
- `EventDispatcher.publish_event` accepts and publishes the event, but does not
  route it by itself. `publish_and_run` accepts, routes, and runs matching queue
  items.
- Keep RPC draft constructors separate from dispatch:
  `rpc_requested_draft`, `rpc_responded_draft`, and `rpc_failed_draft`
  construct wire/protocol lifecycle drafts; route handlers decide whether to
  accept, publish, or route them.

### Capability Execution

File: `src/zeta/capabilities/execution.py`

Current direction:

- Keep `proposed_effect`, `effect_resolution`, and `tool_result_status`-driven
  behavior when they encode capability lifecycle policy.
- Event-id mutation is centralized in `ensure_runtime_event_id`.
- `emit_event` / `emit_tool_event` duplicate run-loop event plumbing. If both
  remain, names should say whether the event is a capability event or a generic
  run event.

### CLI Read Models

File: `src/zeta/cli.py`

Current direction:

- `event_record` is a CLI serialization helper. Do not call it a projection
  unless the CLI has a broader event read model.
- `runtime_state_dir` and `runtime_event_store` are process/CLI wiring helpers,
  not projection helpers. If process assembly owns runtime state paths, move or
  rename them there.

### RPC Event Adapters

File: `src/zeta/rpc/routes.py`

Current direction:

- `rpc_request_id` is borderline: keep it only if fallback-to-event-id is an RPC
  identity policy; otherwise inline the one payload lookup.
- `run_status_from_lifecycle` encodes RPC-visible status policy from lifecycle
  events. Keep it as policy, but consider whether direct event checks are more
  readable than extra helper layers.

### Sigil Status And Trace Rows

Files:

- `src/sigil/status.py`
- `src/sigil/trace/tools.py`

Current direction:

- `history_status_fields` is a read aggregation, not simple field access. Keep
  it if status stays as a separate read model.
- In `sigil/trace/tools.py`, CLI trace row helpers are named around the row
  target they build.
- Trace object id decoding is local and named around the object source.
- `attach_tool_result` and `recovered_tool_error` encode error recovery/display
  policy. Keep them if that fallback behavior stays centralized.

### Session Run Trace Projection

File: `src/zeta/run/thread_run.py`

Current names:

- `_record_trace_for_run`
- `_project_session_trace_result`
- `empty_session_trace_result`

Current direction:

- `_record_trace_for_run` performs a side-effecting trace-store update, so the
  side-effecting name is appropriate.
- `_project_session_trace_result` does return a read-side result derived from
  events plus trace projection maps. It can keep projection language, but the
  target should be the RPC result shape, not the source trace mechanics.

### Agent Schedule Spec Parsing

File: `src/zeta/agents/spec.py`

Current names:

- `schedule_event_type`
- `schedule_timezone_name`

Current direction:

- `schedule_payload` has been inlined.
- `schedule_event_type` and `schedule_timezone_name` are defensible because
  they encode defaults and validation.
- `required_schedule_string` is a validation helper. Keep it only if the shared
  error message remains useful across several schedule fields.

## Separate Noun Refactors

These are related naming issues, but they are not part of the `project_*`
function convention. Track and implement them separately.

### Trace Or Provenance

File: `src/zeta/records/provenance.py`

Question:

- Should the target noun be `TraceProjection` or `ProvenanceProjection`?

Current direction:

- If "trace" is the user-facing artifact and "provenance" is the internal
  explanation model, use `ProvenanceProjection` internally and convert to
  prompt trace artifacts at the boundary.
- If this object is specifically the prompt trace projection, keep the trace
  noun and make the function names explicit about that target.

### History Or Timeline

File: `src/zeta/records/timeline.py`

Question:

- Should `HistoryView` become a Sigil-owned run history read model?

Current direction:

- Move `HistoryView`, history querying, touched-file filters, cost summaries,
  import/export helpers, and Sigil turn/effect record projection to Sigil.
- Do not move canonical run lifecycle event names with the Sigil history read
  model. Those belong under `zeta/run/`.
- Rename the old `turn_*` history names to `run_*` only after deciding whether
  Sigil's user-facing noun is also "run."

### Runtime Context

File: `src/zeta/run/threads.py`

Question:

- Should `SessionScope` become `RuntimeContext`?

Current direction:

- Do not rename it to `ThreadScope`. The object has little to do with a
  conversation thread; it is a runtime resource bundle plus the continuity
  partition used for events and traces.
- Rename `SessionScope` to `RuntimeContext`.
- Rename `src/zeta/run/threads.py` away from thread vocabulary, for example to
  `src/zeta/run/context.py`.
- Move `default_session` and `session_for_id` out of `src/zeta/process.py` or
  rename that module so the construction path reads as runtime-context setup,
  not process-global session management.
- Keep Sigil `session_id` as the shell-continuity noun at the Sigil boundary.
- In Zeta internals, avoid `thread_id` unless a real durable conversation
  thread exists. The id on `RuntimeContext` should describe the runtime
  continuity partition, not an OpenAI-style thread.
- Introduce a separate `Thread` noun only if Zeta later gets a real durable
  conversation object with its own lifecycle/metadata.

## Suggested Order

1. Move run lifecycle event vocabulary out of `records/timeline.py` and into
   `zeta/run/`.
2. Move the Sigil-facing history read model out of Zeta.
3. Clean up `records/provenance.py`.
4. Rename `SessionScope` / `run/threads.py` to `RuntimeContext` /
   `run/context.py`.
5. Handle the noun refactors separately where they unblock function names.
6. Revisit prompt/component projection names once provenance and timeline names
   are stable.
