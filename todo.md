# TODO — corrections from crates/ lifecycle review (2026-08-12)

## Broken

- [x] `zeta-dispatch` compiles again; the `complete_claimed_attempt` /
  `AttemptCompletion` work landed in 7ad36ac and 4e91632.

## Correctness

- [x] **Effect-key fallback scope is retry-stable.** Both runtimes now fall
  back from `effect_scope` to `source_queue_item_id` before the model-minted
  call id, and the `effect_scope_falls_back_to_queue_item` invocation vector
  pins the derived scope and key (f846a4f).
- [ ] **Uncommitted control requests survive as misleading timeline events.**
  Sharpened from "draft idempotency keys are not retry-stable": per-attempt
  model/tool drafts are semantically correct (each attempt is a distinct
  execution), and effect drafts are already durable and retry-stable. The
  actual hole is control intent: `publish_event`/`wait_for`/`cancel` record a
  durable `zeta.tool_call.completed` draft immediately, but the corresponding
  `AgentRequest` lives only in `AgentRunResult` and commits at
  `complete_claimed_attempt`. A crash between run end and completion loses the
  request while the "completed" tool call stays in the session timeline, so
  the retry's model believes it already published and never re-proposes —
  silent event loss. Options: (A) record control intent as durable drafts and
  have attempt completion re-propose uncommitted controls from failed attempts
  of the same queue item (handles are `{qi}:{position}`-stable, so re-commit
  dedupes safely); (B) make the timeline projection attempt-aware so
  failed-attempt control calls are annotated or filtered; (C) both.
  Recommendation: A, since it restores the propose→commit invariant rather
  than teaching the prompt to distrust the journal. Related: the deferred
  recorder-contract defect in notes.md (draft-only recorder cannot return
  retained durable ids) should be fixed in the same slice. Requires
  attempt-recovery policy design — deliberately not implemented ad hoc.
- [x] **Unrouted events are discoverable for recovery.**
  `Dispatch::unrouted_ingress_events` returns stale pending unbound items in
  input order so restart recovery re-drives `route_ingress_event` (ee528ba).
- [x] **Divergent id-duplicate appends fail loudly.** Both runtimes and both
  stores now compare the canonical payload address on id duplicates and fail
  with `duplicate_id_payload_mismatch`; key duplicates keep silent retry
  absorption. journal-v0 §5 amended, operations vectors pin it (bb42aa2).

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
