# TODO — corrections from crates/ lifecycle review (2026-08-12)

## Broken

- [x] `zeta-dispatch` compiles again; the `complete_claimed_attempt` /
  `AttemptCompletion` work landed in 7ad36ac and 4e91632.

## Correctness

- [ ] **Effect-key fallback scope is not retry-stable.** `effect_identity`
  (`crates/zeta-agent/src/runner.rs:1814`) falls back to the model-minted
  `parsed.call_id` as scope when `invocation.effect_scope` is unset, so a
  retried attempt mints a fresh `effect_key` and the provider re-executes the
  side effect — defeating `IdempotentWithKey`. Either make `effect_scope`
  mandatory when any effectful capability is granted, or derive the fallback
  from retry-stable coordinates (`{queue_item_id}:{position}`, like
  `control_handle`).
- [ ] **Draft idempotency keys are not retry-stable.** `model_draft` /
  `tool_draft` (`crates/zeta-agent/src/runner.rs:1377,1405`) embed the UUID
  event id, so journal idempotency never dedupes across attempts: a crashed
  attempt leaves `zeta.tool_call.completed` events for publishes whose
  `AgentRequest` was never committed, and the next attempt's timeline can claim
  work happened that didn't. Decide explicitly which drafts are retry-stable
  and which are per-attempt; make the timeline projection attempt-aware.
- [ ] **Unrouted events block the whole queue with no recovery path.** The
  claim-eligibility SQL (`crates/zeta-dispatch/src/sqlite.rs:2590-2595`) lets
  any earlier `pending` unbound item block every later claim globally. A crash
  between `ingest_event` and `route_ingress_event` stalls the queue forever.
  Either merge ingest+route into one operation or add a recovery sweep that
  re-drives routing for stale pending items.
- [ ] **Duplicate journal appends don't compare payloads.** `MemoryJournal::append`
  (`crates/zeta-journal/src/memory.rs:80-142`) and the SQLite twin return the
  stored event with `inserted: false` without checking the candidate matches —
  divergent payloads under one id are silently dropped. Compare
  `payload_address` on the duplicate path and fail loudly on mismatch.

## Design decisions to make explicit

- [ ] **History repair is part of prompt identity.** Repair rules
  (`crates/zeta-agent/src/prompt.rs:836-1035`) affect prompt object addresses
  and must match the Python twin byte-for-byte. Version them like
  `PromptStructuralTrim:v1` (versioned producer) and/or pin them with a
  dedicated vector suite.
- [x] **The agent-to-Dispatch control handoff is typed.** The root host converts
  `AgentRequest` values into `AttemptControl` proposals, while Dispatch owns
  validation and durable control-array materialization.
- [ ] **Handle-derivation formula is duplicated.** Agent `control_handle`
  (`crates/zeta-agent/src/runner.rs:1854`) and dispatch
  `publish_event_handle`/`wait_handle`
  (`crates/zeta-dispatch/src/identity.rs:269,278`) compute the identical
  `derive(Domain::Chain, "{queue_item_id}:{position}")` with no shared code —
  they agree only by convention. Extract or vector-pin the formula.

## Minor

- [ ] `ProcessExecutor` inherits the provider's stderr
  (`crates/zeta/src/process_executor.rs:322-328`) — will corrupt the terminal
  under a TUI-fronted host. Capture or redirect it.
- [ ] `execution_manifest` recompiles the whole project manifest per agent to
  verify the generation (`crates/zeta-authoring/src/manifest.rs:283-291`) —
  O(project × agents) at startup. Verify once per generation instead.
