# Helper Refactor Rules

## Direction

Helper functions are useful when they name a domain rule, boundary conversion,
normalization policy, or user-visible rendering case. They are suspect when they
only rename field access, hide a dict shape, or exist because nearby code is not
using the domain objects it already has.

## Rules

- Keep helpers that encode a boundary or policy: idempotency keys, durable event
  schemas, provider payload normalization, dispatch/runtime event payloads,
  model-specific message conversion, and user-facing render summaries.
- Inline helpers that only perform field access. If the best name for a helper
  is the field being read, use the field directly.
- Use existing objects as much as possible. Before adding a new projection,
  snapshot, view, or wrapper, check whether the caller can carry the existing
  event, record, runtime object, or provider object directly.
- Split helper-heavy modules only when the helpers serve different domains.
  Function count alone is not the problem; mixed ownership is.
- Keep helper tests focused on behavior and contracts, not on proving that a
  convenience helper mirrors a payload field.
- Let duplication earn abstraction. One caller doing a direct field read is
  fine; repeated validation, normalization, or policy logic can justify a
  helper once the duplication is real.

## Affected Functions

Before changing any Python function here, run the ripple pass for that function
and account for its callers. These are refactor targets, not deletions to apply
blindly.

### Prompt Components

`src/zeta/context/components.py`

- Done: `PromptComponent.message_payload`: inline as `component.message`; this is a
  field lookup on an existing object.
- Done: `PromptComponent.object_links`: inline as `component.links`; this is a field
  lookup on an existing object.
- Done: `PromptComponent.object_data`: keep only if we want `PromptComponent` to own
  trace-object serialization. Otherwise move the body into
  `prompt_component_object` so the object method is not just a convenience
  wrapper.
- Done: `component_messages`: rewrite to read `component.message` directly instead of
  calling `message_payload`.
- Done: `prompt_component_object`: keep as the boundary that turns a
  `PromptComponent` into a stored `Object`, but make it use the object's fields
  directly if the object methods disappear.
- Done: `add_event_link`: inline or replace with local list-building that deduplicates
  links at the construction site.
- Done: `assistant_message_object_id`: inline the `prompt_trace` lookup unless a
  stronger existing object carries this relation.
- Done: `add_trace_object_field`: inline the one-field copy into the source-event
  metadata builders.
- Done: `record_tool_call_ids` and `record_tool_call_names`: consider folding into
  `_chat_message_entries` and `project_timeline_message_components`; they are
  mutable extraction helpers over the same message shape.
- Done: `message_component_kind`: keep only if the role-to-component-kind mapping is a
  named prompt policy; otherwise inline it where components are constructed.

### Source Event Mini-Schema

`src/zeta/context/components.py`

- Done: `timeline_message_component_data`
- Done: `_source_event_metadata`
- Done: `_tool_result_source_event_metadata`
- Done: `_tool_call_source_event_metadata`
- Done: `_model_source_event_metadata`

These functions create a private `source_event` mini-schema inside
`PromptComponent.data`. That schema is then read back by compaction. Prefer using
the existing event/view shape directly, or store the specific stable fields on
`PromptComponent.data` without a second nested event-like object.

`src/zeta/context/compaction/structural_trim.py`

- Done: `source_event`: remove if the prompt-component source-event mini-schema goes
  away; otherwise inline because it is only a typed `component.data` lookup.
- Done: `tool_result`: inline into `structural_trim_payload`; it only reads the
  current source-event shape.
- Done: `tool_call_id` and `tool_name`: first try to satisfy callers from existing
  `PromptComponent` fields/data. If the fallback behavior remains necessary,
  keep them as explicit normalization helpers with names that say they read a
  component, not a generic tool call.
- Done: `project_one_trimmed_message`: rename or inline. It is not a read-model
  projection; it builds the replacement message for one component.

### SQLite Event Store Indexing

`src/zeta/records/stores/sqlite.py`

- Done: `_payload_str`, `_payload_int`, `_payload_json`: replace with use of existing
  queue/attempt objects where practical, or inline at the indexing site. They
  currently make lifecycle event payloads look like an untyped schema API.
- Done: `_runtime_status`: keep only if deriving status from event type is the store's
  indexing policy. Otherwise use the status already present on the
  `QueueItem`/`Attempt` payload.
- Done: `_usage_token`: keep only if the store owns provider-token alias
  normalization; otherwise move this normalization to the producer of the
  attempt result.
- Done: `_optional_str` and `_json_column`: keep as row-decoding helpers only if row
  decoding remains centralized in `_row_to_event` and `_row_to_attempt`;
  otherwise inline into those row mappers.
- Done: `pending_queue_item_id`: keep if this is the queue id policy for unclaimed
  events; otherwise move it next to the queue-item idempotency helpers.

### Queue And Attempt Lifecycle

`src/zeta/orchestration/queue.py`

- Done: `project_one_queue_item`: keep as a real read-model projection only if callers
  need a `QueueItem` reconstructed from lifecycle events. Do not introduce a
  separate queue-item state object.
- Done: `_queue_item_status_from_event`: keep as projection validation if
  `project_one_queue_item` remains; otherwise use the status already carried by
  `QueueItem`.
- Done: `routed_queue_item_from_event` and `queue_item_from_record`: keep because they
  convert concrete source records into `RoutedQueueItem`, but make sure callers
  use `RoutedQueueItem` directly instead of adding another wrapper.
- Done: `terminal_queue_item_event_result`, `terminal_fallback_result`, and
  `result_with_final_cursor`: keep only as terminal-result normalization. Do not
  split them into payload accessors.

`src/zeta/orchestration/attempts.py`

- Done: `attempt_idempotency_key`: keep. This is the kind of policy helper the rules
  are meant to preserve.

`src/zeta/orchestration/dispatch.py`

- Done: `_append_queue_item_event_for_target`: already constructs a `QueueItem`; keep
  using that existing object instead of adding lifecycle payload helpers.
- Done: `_append_attempt_event`: already constructs an `Attempt`; keep using that
  existing object instead of adding attempt payload helpers.
- Done: `event_timestamp`: keep only if UTC ISO formatting is a shared lifecycle
  policy. Otherwise inline at the two lifecycle event construction sites.

### Durable Event Conversion

`src/zeta/records/events.py`

- Done: `draft_from_runtime_event` and `draft_from_boundary_event`: keep as boundary
  conversion helpers; they convert concrete runtime/boundary events into
  `DraftEvent`.
- Done: `model_call_draft`, `tool_call_draft`, `turn_aborted_draft`,
  `stream_chunk_draft`, `status_update_draft`, `user_message_draft`, and
  `durable_event_draft`: keep as draft-event constructors as long as runtime
  writes intentionally go through `DraftEvent`.
- Done: `durable_model_event_payload`, `durable_tool_event_payload`, and
  `durable_payload`: keep only as durable schema construction helpers. Do not
  add matching read-side payload helpers unless repeated validation earns them.
- Done: `event_idempotency_key` and `durable_event_idempotency_key`: keep as
  idempotency policy helpers.
- Done: `tool_result_status`, `normalized_tool_result`, `tool_failure_message`,
  `first_tool_text`, and `flatten_tool_text`: keep as tool-result
  normalization policy.
- Done: `draft_event_id`, `exact_event_time`, `event_timeline_type`,
  `draft_timeline_type`, `durable_view_type`, and `payload_timeline_type`: keep
  only while event views remain dict-shaped. If an existing event/view object
  starts carrying these values, prefer that object over helper calls.

### History Read Model

`src/sigil/history.py`

- Done: `append_draft` and `append_event`: inline unless the history module needs to
  hide `SqliteEventStore` construction for several callers.
- Done: `optional_match`: inline into `turn_matches_filters`.
- Done: `event_time`: prefer one existing event-time helper across Sigil instead of
  duplicating this in both `history.py` and `sessions.py`.
- Done: `domain_payload`: inline into `event_from_record` unless more record-to-event
  converters need the same envelope-stripping policy.
- Done: `project_one_turn_record` and `project_one_effect_record`: keep only as
  read-model projections for `HistoryView`; otherwise carry the durable `Event`
  or the existing history record dict directly.
- Done: `event_from_record` and `event_from_effect_record`: keep as concrete
  record-to-`Event` conversions.
- Done: `turn_sort_key` and `effect_sort_key`: keep if they remain the named ordering
  policy for history views; otherwise inline into the sort calls.

### Trace And Display Rows

`src/sigil/trace/tools.py`

- Done: `tool_call_id_from_object`: inline if callers already have `Object.data`; it
  is a field lookup over the existing `Object`.
- Done: `tool_result_records_by_call_id`: keep if the tool-call table needs this index
  as a named local step.
- Done: `tool_call_row_from_call_object`: keep only if the CLI row is a meaningful
  projection; otherwise construct the row directly in `tool_call_row_from_objects`.
- Done: `attach_tool_result`: keep only if recovered error handling remains coupled to
  row mutation. Otherwise return a completed row from one constructor.

`src/sigil/display/render.py`

- Done: `transcript_prompt_id`: inline the `prompt_trace` lookup or replace it with an
  existing prompt trace object if transcript rendering starts carrying one.
- Done: `transcript_tool_result_lines`: keep only as a rendering case; do not turn it
  into a generic result accessor.
- Done: `transcript_results_index`: keep as a named transcript-joining step.

### Runtime And RPC Boundaries

`src/zeta/run/runtime.py`

- Done: `model_event_payload`: keep as the general name for the model runtime payload
  rule.
- Done: `assistant_tool_calls`: inline only if it remains a single local filter;
  otherwise keep as normalization of provider assistant payloads.
- Done: `add_prompt_trace_fields`, `add_model_prompt_trace_fields`,
  `projected_tool_call_ids`, and `add_tool_result_trace_fields`: prefer using
  the existing `PromptTraceProjection` object directly. If these stay, keep them
  as projection-application helpers, not field accessors.
- Done: `emit_event` and `emit_tool_event`: consider inlining if they remain thin list
  append/sink wrappers.

`src/zeta/rpc/routes.py`

- Done: `rpc_request_id`: inline if it is only a fallback field lookup; keep if the
  fallback-to-event-id behavior is a protocol rule.
- Done: `rpc_message_from_event`: keep as `event_from_*`/`*_from_event` style
  conversion from a durable event into a JSON-RPC message.
- Done: `event_to_wire` and `capability_to_wire`: keep as concrete object-to-wire
  conversions.
- Done: `invalid_params`: keep as a stable JSON-RPC error constructor.

### Tests To Loosen With The Refactor

- Tests that assert `PromptComponent.message_payload`, `object_data`, or
  `object_links` should move to the behavior of stored prompt objects and model
  messages.
- Tests that call `chat_messages`, `component_messages`,
  `model_tool_call_event_payload`, or `queue_item_status_counts` directly should
  be checked during deletion/inline passes. Keep them only if these functions
  remain public behavior, not convenience helpers.
