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

### Records Event Helpers

File: `src/zeta/records/events.py`

Current direction:

- Keep idempotency helpers (`event_idempotency_key`,
  `durable_event_idempotency_key`) when they encode acceptance/dedupe policy.
- Keep the `*_draft` suffix for functions that construct a `DraftEvent` and
  centralize an emitted event schema.
- Current schema constructors:
  `draft_from_runtime_event`, `draft_from_boundary_event`, `model_call_draft`,
  `tool_call_draft`, `turn_aborted_draft`, `stream_chunk_draft`,
  `status_update_draft`, `user_message_draft`, and `durable_event_draft`.
- `turn_aborted_draft` now uses the failed lifecycle event name from
  `zeta.run.events`, but the constructor still lives in `records/events.py`.
  Move it only if the `DraftEvent` dependency can move without circular imports.
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

## Separate Noun Refactors

These are related naming issues, but they are not part of the `project_*`
function convention. Track and implement them separately.

### History Or Timeline

File: `src/sigil/history.py`

Question:

- Should `HistoryView` become a Sigil-owned run history read model?

Current direction:

- `HistoryView`, history querying, touched-file filters, cost summaries,
  import/export helpers, and Sigil turn/effect record projection now live in
  Sigil.
- Do not move canonical run lifecycle event names with the Sigil history read
  model. Those belong under `zeta/run/`.
- Rename the old `turn_*` history names to `run_*` only after deciding whether
  Sigil's user-facing noun is also "run."

## Suggested Order

1. Handle the noun refactors separately where they unblock function names.
2. Revisit prompt/component projection names once timeline names
   are stable.
