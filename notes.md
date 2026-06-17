# Sigil Zeta cleanup summary

Status: complete.

This file previously tracked the step-by-step execution plan for the Zeta
runtime cleanup. The work is now finished and pushed, so the active journal has
been collapsed into this summary.

## Completed scope

1. Removed the trace `run_event` chain as the parallel timeline source.
2. Made the Zeta RPC syscall surface explicit and enforceable.
3. Reworked tools into declared capabilities mediated by the capability
   registry and per-run projections.
4. Made model input/output provider-neutral inside Zeta.
5. Split prompt construction into plan, commit, and provider render phases.
6. Reworked the agent loop into an explicit step-engine shape with capability
   lifecycle statuses, terminal result reconciliation, and abort-step
   visibility.
7. Promoted replay, trace graph, staged mutation, aborted run, and durable event
   object-link invariants into acceptance tests.
8. Sharpened the Zeta/Sigil boundary:
   - `src/zeta` remains product-neutral and does not import `sigil`;
   - Zeta owns `zeta.*` event/history names;
   - Sigil owns project-context loading, CLI workflows, local capability
     registration, and Sigil-specific event provenance.

## Final verification

- `uv run pytest -q` passed with 829 tests and 4 skipped.
- `uv run coverage run -m pytest` and `uv run coverage report` passed with 93%
  total coverage.
- `uv run ty check src tests` passed.
- `uv run pre-commit run --all` passed.

## Notes

- `ripple` was required by the repository workflow but was not installed in this
  checkout, so the attempted ripple checks could not run.
- Claude and Gemini second-opinion checks were attempted where required for
  complex boundary/refactor planning; Claude was unauthenticated and Gemini was
  not installed.
- The untracked `.worktrees/` directory was left untouched.
