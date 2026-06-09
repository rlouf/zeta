# Zeta Context Budget Layer

Zeta prompt construction is moving toward a budget-constrained materialized
view over the trace store. The current implementation establishes the seams for
that design without adding eviction policy or a retrieval tool.

## Layers

The trace store is the durable source of prompt provenance. Prompt components,
model prompts, assistant messages, tool calls, and tool results are stored as
content-addressed objects, with derivations recording how one object was
produced from others. SQLite tracing is still fail-open: if storage fails, the
turn continues with an ordinary prompt and the trace logger emits one warning
per failed operation.

Derivations are the provenance edge. Prompt transforms that produce new
components link back to their source component object ids, so later tooling can
explain or replay where a compact representation came from.

The budget layer is currently measurement only. `estimated_tokens(component)`
uses a deterministic chars/4 heuristic, and `measure(components)` returns total
usage plus a per-component breakdown. `ContextBudget` carries the maximum token
number used by threshold gates. There is no planner and no eviction decision in
this layer.

Future planner and fault-handler work will choose representation levels for
components: `full`, `summary`, `stub`, or `absent`. The model-facing prompt will
be materialized from that plan, and a future expand tool can use trace ids to
re-expand stubs when needed.

## Stub Contract

All stub messages use one canonical rendering:

```text
[elided {kind} {n_tokens}~tok id={object_id} — content retrievable by id]
```

The `object_id` is the trace object that can be retrieved later. Structural trim
currently emits `stub` components using this format. Task-state extraction emits
`summary` components, not stubs.

## Runtime Switches

Prompt transforms are off by default. Production agent construction reads:

```text
ZETA_TRIM=off|structural|task_state
ZETA_TRIM_THRESHOLD_TOKENS=100000
```

The threshold gate uses `measure()` and only runs the configured transform when
the measured prompt exceeds the configured token threshold.
