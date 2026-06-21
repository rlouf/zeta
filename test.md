# Lean Test Suite Plan

## Goal

Rewrite the deleted test suite as a small behavior-pinning suite.

We do not want broad coverage for its own sake. We want a compact set of tests
that makes real simplification safer by pinning only the behaviors that external
callers and users rely on.

The suite should be split by ownership:

- `tests/zeta/` covers the Zeta runtime boundary.
- `tests/sigil/` covers the Sigil user-facing boundary.
- `tests/shared/` contains only tiny harness helpers that are genuinely shared.

Target size for the first rewritten suite:

- about 10-14 test files
- about 50-80 tests total
- no large catch-all file
- no test file over roughly 250 lines without a strong reason

If a proposed test does not protect a behavior that would matter to a user,
client, persisted-data reader, or provider adapter, do not write it.

## Core Rule

Pin behavior, not internal shape.

A test may pin shape only when that shape is observable behavior:

- stdout or stderr text
- process exit code
- JSON-RPC request, response, error, or notification payload
- model provider request payload
- persisted event, turn record, effect record, or trace object representation
- shell command text emitted by the binding
- file contents, patch contents, or tool side effects
- public authored config/spec semantics

A test should not pin shape for:

- private dataclasses
- helper return values
- temporary dicts passed between internal functions
- tuple vs list inside Python-only state
- exact ordering of internal calls
- exact exception class for an internal-only invalid call
- private event construction before the event crosses a durable or protocol
  boundary

When in doubt, test through the narrowest public observation that proves the
behavior. For example, test that `session.run` emits the right requested-turn
event or JSON-RPC result, not that `SessionRunParams.tools` is a tuple.

## What Not To Rebuild

Do not recreate the old suite in smaller folders.

Do not write tests just because the old suite had a case for it.

Do not write tests for:

- every branch of defensive validation
- every helper function
- every CLI help line
- every display formatting variant
- every model stream edge case
- every shell quoting corner
- every trace query mode
- every builtin tool option
- internal type coercion
- compatibility behavior we intentionally removed

Prefer one representative behavior test per contract. Add another only when it
protects a materially different failure mode.

## Test Budget

Use this budget as a hard default. Exceed it only after writing down why the
extra tests protect distinct observable behavior.

| Area | Files | Target tests |
|---|---:|---:|
| Global fixtures/shared helpers | 3-4 | helper code only |
| Zeta contracts | 3 | 15-22 |
| Zeta runtime/model smoke | 2-3 | 10-16 |
| Sigil CLI/workflow | 3 | 12-18 |
| Sigil shell/tools/state | 3-4 | 12-20 |

Initial total target: 50-80 tests.

## Sigil Must Keep Working

The lean suite is only acceptable if it protects the behaviors that make Sigil
feel good today. These are not optional.

The first rewritten suite must include a small Sigil golden path that exercises
real user workflows through public entrypoints:

- [ ] `ask` can answer a piped question without polluting stdout with trace
  output.
- [ ] `step` can propose a mutating action without executing it immediately.
- [ ] `do` can execute a mutating action directly.
- [ ] a staged shell command can be resumed by the matching shell command.
- [ ] history/status can report the last meaningful turn.
- [ ] the shell binding routes comma, plus, and status glyphs to the public CLI
  behavior.
- [ ] the read/search/mutate tools work on real temporary files.

If these pass, Sigil has a high-signal safety net. If any are missing, the suite
may be lean, but it is not protecting the product.

Keep this golden path small. Do not expand it into a matrix of every option.
One realistic path per workflow is better than twenty brittle unit tests.

## Target Layout

Keep the first pass intentionally small:

```text
tests/
  conftest.py

  shared/
    events.py
    models.py
    rpc.py
    cli.py

  zeta/
    conftest.py
    test_events.py
    test_rpc.py
    test_runtime.py
    test_models.py
    test_trace.py

  sigil/
    conftest.py
    test_cli.py
    test_workflows.py
    test_shell.py
    test_tools.py
    test_state.py
```

Do not split into many files until a file becomes hard to scan. A file can cover
a domain if each test is behavior-oriented and short.

Keep the separation strict:

- Zeta tests should not import Sigil workflow modules unless the behavior being
  tested is explicitly Sigil-to-Zeta integration.
- Sigil tests may exercise Zeta through public integration points, but should
  not assert Zeta internals.
- Shared helpers must stay behavior-oriented and small. If a helper is only used
  by one side, keep it under that side's `conftest.py`.

## Todo List

Follow this in order.

### 0. Confirm The Starting Point

- [ ] Run `git status --short`.
- [ ] Confirm the old `tests/` suite is gone.
- [ ] Read this file before creating tests.
- [ ] Do not inspect deleted test files from Git history unless source behavior
  is genuinely unclear.
- [ ] Inspect `src/` modules directly when deciding behavior.
- [ ] Keep a running count of test files and test functions. If the count starts
  drifting past the budget, stop and prune.

### 1. Create Minimal Global Isolation

Create `tests/conftest.py`.

Add one autouse fixture that isolates:

- [ ] `HOME`
- [ ] `SIGIL_STATE_DIR`
- [ ] `ZETA_STATE_DIR`
- [ ] `SIGIL_SESSION_ID`
- [ ] `SIGIL_SESSION_DIR`
- [ ] `ZETA_SESSION_ID`

Rules:

- [ ] Use `tmp_path` or `tmp_path_factory`.
- [ ] Do not read or write real user state.
- [ ] Do not put domain helpers here.
- [ ] Run `uv run pytest -q` after adding it.

### 2. Add Shared Helpers Only On Demand

Create these only when the first test needs them:

- [ ] `tests/shared/events.py`
- [ ] `tests/shared/rpc.py`
- [ ] `tests/shared/models.py`
- [ ] `tests/shared/cli.py`

Rules:

- [ ] Helpers may build inputs.
- [ ] Helpers may parse public outputs.
- [ ] Helpers may assert public envelopes.
- [ ] Helpers must not expose private runtime state as the test oracle.
- [ ] If a helper has more than three call sites in one file only, keep it local
  instead of shared.

### 3. Write Zeta Contract Tests

Create `tests/zeta/test_events.py`.

Write only these tests:

- [ ] durable event view preserves public type/session/turn/cursor behavior
- [ ] model/tool/tool-result runtime events project to durable public views
- [ ] event store appends and lists in cursor order for SQLite
- [ ] event store filters by session and turn
- [ ] idempotent append returns the existing event

Do not test every event helper. Do not test both memory and SQLite unless the
source behavior differs materially. SQLite is the more important contract.

Create `tests/zeta/test_rpc.py`.

Write only these tests:

- [ ] `initialize` returns the public server metadata
- [ ] unknown method returns a JSON-RPC error
- [ ] `events.list` returns filtered events with cursor behavior
- [ ] `tools.register` exposes a client tool and duplicate registration fails
- [ ] client tool call emits `tools.call` and consumes `tools.respond`
- [ ] `session.run` returns a lifecycle result for a fake runner
- [ ] `session.cancel` cancels an active run or reports no active run

Do not test every malformed JSON-RPC input. One malformed/unknown-method path is
enough unless another path protects a different public error contract.

Run:

```bash
uv run pytest tests/zeta/test_events.py tests/zeta/test_rpc.py -q
```

### 4. Write Zeta Runtime And Provider Smoke Tests

Create `tests/zeta/test_runtime.py`.

Write only these tests:

- [ ] text-only model answer finalizes with a final answer
- [ ] model tool call invokes an allowed tool and feeds the result into the next
  turn
- [ ] disallowed tool is refused and not invoked
- [ ] staged mutating tool stops with a staged effect
- [ ] direct mutating tool can continue
- [ ] cancellation or wall-clock budget records an abort outcome

Create `tests/zeta/test_models.py`.

Write only these tests:

- [ ] chat-completions request body contains model, messages, tools, and
  reasoning fields when configured
- [ ] chat-completions stream reconstructs one text response and one tool call
- [ ] Responses request body converts system/tool/model messages correctly
- [ ] model profile resolution prefers session selection, then configured
  default, then builtin fallback

Create `tests/zeta/test_trace.py`.

Write only these tests:

- [ ] trace object IDs are stable for equivalent data
- [ ] durable model/tool events project into a trace graph sufficient for replay
- [ ] replay can reconstruct one model/tool exchange

Run:

```bash
uv run pytest tests/zeta -q
```

### 5. Write Sigil CLI And Workflow Tests

Create `tests/sigil/test_cli.py`.

Write only these tests:

- [ ] top-level help exposes the main command groups
- [ ] `ask` keeps stdout clean for a piped answer
- [ ] `status --json` exposes model/session state
- [ ] model selection command stores and reports the active profile
- [ ] session transcript renders a simple conversation

Create `tests/sigil/test_workflows.py`.

Write only these tests:

- [ ] `step` stages a mutating tool handoff instead of executing it
- [ ] `do` executes a mutating tool directly
- [ ] resolved shell handoff appends a tool result
- [ ] workflow records an answered/staged/executed turn outcome
- [ ] trace/progress output stays separate from final answer

Run:

```bash
uv run pytest tests/sigil/test_cli.py tests/sigil/test_workflows.py -q
```

### 6. Write Sigil Shell, Tool, And State Tests

Create `tests/sigil/test_shell.py`.

Write only these tests:

- [ ] zsh binding routes comma/plus/status glyphs to the public CLI behavior
- [ ] glyph parsing preserves raw user text
- [ ] shell recording skips Sigil commands and records normal shell commands
- [ ] staged command resume matches the originating staged command

Create `tests/sigil/test_tools.py`.

Write only these tests:

- [ ] read tool reads text with offset/limit behavior
- [ ] grep tool returns matches with enough metadata to ground edits
- [ ] bash stages by default and executes in direct mode
- [ ] write/edit direct mode changes file contents and staged mode returns a
  reviewable effect
- [ ] query-log reports an empty history and one cited turn

Create `tests/sigil/test_state.py`.

Write only these tests:

- [ ] history records turn and effect outcomes
- [ ] failure context stores redacted snippets and is cleared after success
- [ ] session IDs are traversal-safe
- [ ] bundle export/import round-trips one session with trace closure
- [ ] spool ingestion records one shell command and skips malformed records

Run:

```bash
uv run pytest tests/sigil -q
```

### 7. Prune Before Expanding

Before adding any extra test, ask:

- [ ] Would this catch a user-visible or client-visible regression not caught by
  an existing test?
- [ ] Is this testing behavior rather than implementation?
- [ ] Is the failure mode likely enough to justify permanent test weight?
- [ ] Can this be covered by strengthening an existing test instead?
- [ ] Does this test make a future simplification harder for no product reason?

If any answer is uncomfortable, do not add the test.

### 8. Final Verification

- [ ] Run `uv run pytest -q`.
- [ ] Run `uv run ruff check`.
- [ ] Run `uv run ty check`.
- [ ] Run `uvx --with radon radon cc src tests -s`.
- [ ] If docs or config changed, run `uv run pre-commit run --all`.
- [ ] Run `git status --short`.
- [ ] Confirm no untracked cache files are left under `tests/`.
- [ ] Confirm test names describe behavior, not implementation.
- [ ] Confirm Zeta tests do not import Sigil internals except explicit
  integration tests.
- [ ] Confirm Sigil tests do not assert Zeta internal shapes.
- [ ] Confirm each exact dict/list assertion is for an observable boundary.
- [ ] Count tests. If the first rewrite is above 80 tests, prune before
  committing.

## Validation Policy Decisions

Decide these once before writing tests. Then pin the decision only at the
relevant behavior boundary.

| Question | Boundary | Default recommendation |
|---|---|---|
| Should malformed JSON-RPC params return structured errors? | Zeta RPC | Yes, but test one representative malformed path |
| Should internal Python call sites validate argument types? | Internal | No |
| Should `session.run.objective` require a string? | Zeta RPC/session | Yes, unless raw JSON values are intentionally accepted |
| Should `events.list.after` require a string cursor? | Zeta RPC | Yes |
| Should `events.list.limit` require an integer? | Zeta RPC | Yes |
| Should client tools require schemas? | Zeta RPC/tools | No, unless model provider behavior needs strict schemas |
| Should capability schemas be validated at registration? | Zeta capabilities | No, unless registration is a public config boundary |
| Should tool-call args be JSON-schema validated before invocation? | Zeta runtime/tools | No, unless this is a product safety policy |
| Should authored specs reject wrong field types? | Zeta agents | Yes for user-authored files, but do not exhaustively test wrong types |
| Should model profile TOML reject wrong field types? | Zeta models | Yes for user-authored config, but one malformed-config test is enough |
| Should old persisted events stay readable? | Zeta/Sigil storage | Yes, but test one representative old event shape |

## Coverage Targets

The goal is contract confidence, not line coverage.

Minimum acceptable coverage:

- every public Zeta JSON-RPC method family has at least one behavior test.
- Zeta event storage has append/list/filter/idempotency coverage.
- Zeta agent loop has text, tool, staged, direct, refused, and abort paths.
- Zeta model adapters have one exact request payload test and one stream
  reconstruction test.
- Zeta trace replay can reconstruct one model/tool exchange.
- Sigil golden path covers `ask`, `step`, `do`, shell resume, status/history,
  shell glyph routing, and real-file tools.
- Sigil CLI has one behavior test for the command groups that users touch every
  day.
- Sigil shell has static binding behavior and one resume path.
- Sigil builtin tools have representative read/search/mutate/query behavior on
  real temporary files.
- Sigil history records round trip at least one meaningful turn.

## Anti-Goals

Do not rebuild:

- a mirror of the current test file layout.
- a giant shared helper module.
- broad monkeypatch-heavy tests of private helpers.
- tests that exist only to make coverage numbers high.
- compatibility tests for behavior intentionally removed.
- validation tests for internal call paths.
- a case matrix for every option.

## Expected End State

After the rewrite, a refactor should be able to:

- rename private helpers,
- collapse dataclasses,
- remove internal validators,
- simplify runtime plumbing,
- change internal event construction steps,
- reorganize modules,

without changing tests, as long as observable behavior remains true.

The suite should still fail immediately when:

- Zeta JSON-RPC clients would break,
- Sigil CLI behavior changes,
- persisted events become unreadable,
- trace replay breaks,
- model provider payloads drift,
- shell dispatch changes,
- tools perform the wrong side effect,
- user-authored config/spec semantics change.
