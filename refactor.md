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
