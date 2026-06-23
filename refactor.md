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

### Orchestration Queue

File: `src/zeta/orchestration/queue.py`

- Rename `queue_item_snapshots` to `project_queue_items`.
- Rename `queue_item_snapshot_from_event` to `project_one_queue_item`.
- Rename `queue_item_event_status` to `queue_item_status_from_event` or make it
  private if it is only used by `project_one_queue_item`.

The target type decision is tracked under "Separate Noun Refactors."

- Re-evaluate `required_payload_string`, `optional_payload_string`, and
  `queue_item_result`. They currently read like generic field-access helpers.
  Inline them unless they become shared schema decoders with a neutral home.
- Keep `queue_item_idempotency_key` and
  `unhandled_queue_item_idempotency_key` only if they define event dedupe
  policy; consider renaming them to `*_event_key` if that is the real contract.
- Re-evaluate `queue_item_payload` and `terminal_queue_item_status` with the
  same rule as attempts: inline mechanical payload assembly, keep/rename
  lifecycle policy.

### Orchestration Attempts

File: `src/zeta/orchestration/attempts.py`

Current names:

- `AttemptSnapshot`
- `attempt_snapshots`
- `attempt_snapshot_from_event`
- `attempt_event_status`
- `attempt_status_counts`

Target direction:

- Delete the attempt projection object and helpers instead of renaming them.
- The event log already carries the attempt lifecycle data. Do not introduce a
  separate read object unless a caller needs normalized attempt state.
- Inline the only production read where it is needed: `_next_attempt_number`
  can scan `runtime.attempt.*` events, filter by `queue_item_id`, read
  `attempt_number` from the payload, and return `max(..., default=0) + 1`.
- Keep `attempt_idempotency_key` because it encodes event dedupe policy, not a
  field lookup.
- Re-evaluate `attempt_id_for_queue_item`, `attempt_payload`,
  `attempt_result_payload`, and `terminal_attempt_status`. If they only wrap
  one local lookup or a mechanical `asdict`/payload shape, inline them. If they
  encode lifecycle event schema or terminal status policy, rename them so that
  policy is visible.
- Delete tests that only pin `AttemptSnapshot` projection behavior unless they
  are replaced by behavior-level retry/attempt-number tests.

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

### Run Prompt Projection

File: `src/zeta/run/runtime.py`

Current names:

- `add_projection_fields_for_prompt`
- `add_model_projection_fields`
- `add_tool_result_projection_fields`
- `update_prompt_trace_from_projection`

Target direction:

- These functions mutate prompt trace/projection payloads and should either move
  closer to `context` / `records/provenance.py` or be renamed around the target
  they produce.
- If they remain projection helpers, prefer names like
  `project_one_prompt_trace_model_event` or keep them private under the
  top-level trace projector.
- Avoid generic `add_*_projection_fields` names because they describe mechanics,
  not the read model being produced.
- `draft_timeline_type` duplicates the timeline-type rule in records
  (`event_timeline_type` / `durable_view_type`). Consolidate the rule near
  records/provenance instead of carrying parallel local helpers.
- `draft_event_id` also exists in `records/events.py`. Prefer one canonical
  helper if idempotency-key parsing remains a shared event contract.
- `model_event_payload` and `assistant_tool_calls` are only worth keeping if
  they encode run event schema. Inline or move them if they are just local dict
  construction.

### Context Components

File: `src/zeta/context/components.py`

Current names that look like projection/conversion points:

- `prompt_components`
- `timeline_message_components`
- `non_message_components`
- `chat_messages`
- `component_messages`
- `timeline_chat_message`
- `role_or_event_chat_message`
- `structured_*_event`

Target direction:

- Apply the convention where a function projects timeline records/events into
  prompt components:
  `timeline_message_components` could become
  `project_timeline_message_components`.
- Single-event/component helpers should follow `project_one_*` only when they
  are actual projectors. For example, `role_or_event_chat_message` should become
  a target-oriented name such as `project_one_chat_message` if it projects one
  timeline entry into a chat message.

### SQLite Store Projections

File: `src/zeta/records/stores/sqlite.py`

Current names:

- `_project_session_mapping`
- `_project_runtime_event`
- `_project_queue_item_event`
- `_project_attempt_event`
- `_project_attempt_result`

Target direction:

- These are write-side index updates, not pure read-model projectors.
- Rename them to make the target index explicit:
  `_index_one_session_mapping`, `_index_one_queue_item`,
  `_index_one_attempt`, `_index_one_attempt_result`.

### Sigil Runtime Draft Projection

File: `src/sigil/agent_io.py`

Current names:

- `project_trace_for_turn`
- `project_runtime_draft`

Target direction:

- Rename `project_trace_for_turn` so the side effect is visible if it writes to
  a store, for example `record_trace_for_turn` or `update_trace_for_turn`.
- `project_runtime_draft` should be checked carefully. If it converts one draft
  into a durable/user-facing draft, rename it to `project_one_runtime_draft` or
  choose a more specific target noun.

### Capability Projection

File: `src/zeta/capabilities/registry.py`

Current direction:

- If this remains a projection concept, add projector functions that follow the
  convention:
  `project_capability_tools` / `project_one_capability_tool`.
- The `CapabilityProjection` type decision is tracked under "Separate Noun
  Refactors."
- `CapabilityRegistry.project` is not just field access: it resolves capability
  ids, handles name overrides, detects ambiguous names, and builds provider
  descriptors. Rename it around the model-visible target or provider-schema
  target.

### Context Compaction Projection Helpers

File: `src/zeta/context/compaction/structural_trim.py`

Current names:

- `is_tool_result_projection`
- `trimmed_message_projection`

Target direction:

- If they produce projected messages, use the convention:
  `project_one_trimmed_message`.
- `is_tool_result_projection` is an inspector, not a projector. Rename around
  what it detects if the current word "projection" is misleading.
- `structural_trim_payload` encodes trace/audit metadata for trimmed content,
  so it may be worth keeping. The name should say it is trim metadata if that
  is the policy being centralized.

### Project Directory Loading Conflict

File: `src/zeta/process.py`

Current name:

- `project_specs`

Target direction:

- Rename it away from the reserved `project_*` prefix, for example:
  `load_project_specs`, `load_agent_specs`, or `agent_specs_for_project`.

### Scheduling

File: `src/zeta/orchestration/scheduling.py`

Current direction:

- Re-evaluate `utc_now` and `schedule_current_time`. `utc_now` is a one-line
  clock wrapper; inline it unless tests need injection through a named seam.
  `schedule_current_time` may be worth keeping only if the timezone conversion
  policy is reused or needs focused tests.
- `schedule_event_payload` encodes the emitted schedule event schema. Keep it
  if schedule payload construction appears in more than one place; otherwise
  inline it inside `emit_due_schedules`.

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
- Rename current plain-dict event builders around this rule. Examples:
  `model_event` -> `model_event_payload`,
  `model_tool_call_event` -> `model_tool_call_event_payload`,
  `tool_result_event` -> `tool_result_event_payload`, and
  `shell_result_event` -> `shell_result_event_payload`.
- `optional_event_string` is a field-validation helper. Inline it unless there
  is enough repeated validation pressure to justify a shared decoder.
- `durable_view_type`, `event_timeline_type`, and `draft_timeline_type` express
  the same timeline-type rule in different modules. Consolidate the rule in one
  records/provenance location.
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
- Rename persistence functions that take an existing draft instead of creating
  one:
  `_record_runtime_draft` and `record_runtime_draft` should become names like
  `record_runtime_event` or `accept_runtime_event_draft`.
- Delete `project_runtime_draft`; it is currently an identity function.
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
- `result_staged_effect` is a wrapper over `proposed_effect`; delete it unless
  callers need a different semantic name.

### CLI Read Models

File: `src/zeta/cli.py`

Current direction:

- `event_record` is a CLI serialization helper. Do not call it a projection
  unless the CLI has a broader event read model.
- `queue_status_counts` is small aggregation logic for display. Inline it if it
  remains local to one command, or rename around the rendered status summary if
  it becomes shared.
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

### Sigil Event Adapters

Files:

- `src/sigil/state.py`
- `src/sigil/handoff.py`
- `src/sigil/turn.py`
- `src/sigil/agent_io.py`

Current direction:

- `optional_string`, `event_id_value`, `handoff_event_payload`,
  `handoff_event_time`, and `handoff_event_turn_id` look like field-access
  helpers. Inline them unless repeated validation pressure justifies them.
- `project_runtime_draft` should be rechecked against the naming convention. If
  it only tags or converts one draft, either rename it to the target it produces
  or inline it at the caller.

### Sigil Status And Trace Rows

Files:

- `src/sigil/status.py`
- `src/sigil/trace/tools.py`

Current direction:

- `history_status_fields` is a read aggregation, not simple field access. Keep
  it if status stays as a separate read model.
- In `sigil/trace/tools.py`, `tool_call_row`,
  `tool_results_by_call_id`, and `base_tool_call_row` are CLI read-model
  construction. Rename them with clearer `row`/`view` vocabulary unless they
  become general trace projections.
- `object_data`, `result_fields`, and `tool_call_id` are field-access helpers.
  Inline them unless their validation is reused enough to justify local
  decoders.
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

- `schedule_payload`
- `schedule_event`
- `schedule_timezone`

Current direction:

- `schedule_payload` is currently a cast around one YAML field. Inline it unless
  schedule payload validation becomes real.
- `schedule_event` and `schedule_timezone` are more defensible because they
  encode defaults and validation. If they stay, names like
  `schedule_event_type` and `schedule_timezone_name` would make the returned
  schema field clearer.
- `required_schedule_string` is a validation helper. Keep it only if the shared
  error message remains useful across several schedule fields.

### Sigil Turn Effect Field Builders

File: `src/sigil/turn.py`

Current names:

- `tool_result_effect_fields`
- `file_effect_fields`
- `command_effect_fields`
- `event_id_value`
- `first_object_link_id`

Current direction:

- `tool_result_effect_fields`, `file_effect_fields`, and
  `command_effect_fields` encode the policy that maps tool results to history
  effects. Prefer target-oriented names like `effect_fields_for_tool_result`.
- `event_id_value` is a plain field accessor. Inline it if still used.
- `first_object_link_id` is a small traversal helper over trace-link schema.
  Keep it only if it is reused or if the trace-link schema needs one local
  interpretation point.

### Display Progress Read Models

File: `src/sigil/display/state.py`

Current names:

- `progress_event_for_tool_result`
- `progress_event_for_tool_call`
- `mutation_progress_event`
- `command_progress_event`
- `progress_subject_fields`

Current direction:

- `progress_subject_fields` is lookup policy for display subjects. Keep it if
  the mapping stays centralized; otherwise inline small per-tool cases.

## Separate Noun Refactors

These are related naming issues, but they are not part of the `project_*`
function convention. Track and implement them separately.

### Queue Item Projection Target

File: `src/zeta/orchestration/queue.py`

Question:

- Do we need `QueueItemSnapshot` at all?

Current direction:

- The extra fields look unnecessary for queue item projection.
- Prefer deleting `QueueItemSnapshot` and projecting directly to `QueueItem`.
- Keep terminal result/error/cursor behavior in explicit terminal-result
  helpers, not in the queue item projection.

The resulting projection target would be:

```python
QueueItem(
    queue_item_id=...,
    event_id=...,
    target_agent=...,
    status=...,
)
```

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
- Keep Sigil `session_id` as the shell-continuity noun at the Sigil boundary.
- In Zeta internals, avoid `thread_id` unless a real durable conversation
  thread exists. The id on `RuntimeContext` should describe the runtime
  continuity partition, not an OpenAI-style thread.
- Introduce a separate `Thread` noun only if Zeta later gets a real durable
  conversation object with its own lifecycle/metadata.

### Capability Projection Type

File: `src/zeta/capabilities/registry.py`

Question:

- Is `CapabilityProjection` really a projection, or is it a provider-facing
  tool schema?

Current direction:

- If it is the provider-facing schema, rename it to a target noun such as
  `CapabilityToolSchema`.
- If it remains a projection concept, keep the projection noun and add
  `project_*` functions around it.

## Suggested Order

1. Apply the convention to `orchestration/queue.py`.
2. Apply the same shape to `orchestration/attempts.py`.
3. Move shared event payload string readers out of `queue.py`.
4. Rename `project_specs` so `project_*` is reserved for projection.
5. Move run lifecycle event vocabulary out of `records/timeline.py` and into
   `zeta/run/`.
6. Move the Sigil-facing history read model out of Zeta.
7. Clean up `records/provenance.py`.
8. Rename `SessionScope` / `run/threads.py` to `RuntimeContext` /
   `run/context.py`.
9. Handle the noun refactors separately where they unblock function names.
10. Revisit prompt/component projection names once provenance and timeline names
   are stable.
