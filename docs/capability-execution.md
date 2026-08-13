# Capability refactor plan

**Status: foundation complete in `5f0c33d`.** Each capability now has one
execution path. The session request has no client execution selector.

## Current facts

- `Capability` declares `delivery_semantics` when retry behavior needs it.
- The agent or host selects its allowed tool set.
- A tool result can set `stop: true`. The run then ends with `tool_stop`.
- Effect records use the declared delivery semantics.
- The runtime validates tool arguments before it calls an executor.
- The runtime validates each executor result before it records the result.
- Each run resolves its executor before the model loop starts.

## Current gaps

`CapabilityRegistry` still combines three jobs: name resolution, model tool
descriptors, and implementation dispatch.

`registered_capabilities()` silently omits an unknown requested capability.
That can start a run with fewer tools than its author declared.

The Responses adapter converts Chat Completions tool descriptors. Each API
should receive a tool envelope from one shared renderer.

## Target boundaries

### Grants

Build one immutable capability grant for each run. The grant maps each
model-visible name to one capability declaration.

Grant creation must fail for an unknown name, an ambiguous name, or a bad
prefix glob. A host selects its allowed tool set through this grant.

### Rendering

Render the grant for each model API. The renderer changes only the outer
envelope. It does not change the declared JSON Schema.

### Dispatch

Each run resolves one `CapabilityExecutor` before dispatch. The local executor is
the default when agent Markdown has no `executor:` field.

The executor receives a canonical capability id, validated arguments, a base
directory, and an effect key.

## Invariants

1. The model can call only a granted tool.
2. Each granted name resolves before a run starts.
3. Each run has one selected executor before tool dispatch.
4. Dispatch passes the granted canonical id and validated arguments.
5. Each tool result includes `ok: bool`.
6. The runtime validates an executor result before it records the result.
7. A side effect has a retry-stable identity.
8. Each model backend receives its own tool envelope.
9. Each model tool call maps to one granted capability.

## Delivery order

1. Add grant tests for unknown names, ambiguous names, and prefix globs.
2. Add `capabilities/grant.py` and build the grant in `run_agent_loop`.
3. Add renderer tests for Chat Completions and Responses.
4. Add `capabilities/render.py` and remove adapter conversion code.
