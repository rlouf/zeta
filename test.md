# Clean-Slate Test Suite Plan

## Goal

Delete the current test suite and rewrite it from scratch to pin observable
behavior only.

The new suite should be split by ownership:

- `tests/zeta/` pins the Zeta runtime, protocol, event, model, prompt, trace,
  and agent behavior.
- `tests/sigil/` pins the Sigil CLI, shell integration, workflows, history,
  display, install, and builtin user tools.
- `tests/shared/` contains only test harness pieces that are genuinely shared
  across both sides.

The suite should not mirror the current source tree blindly. It should answer:

> What behavior can an outside caller, CLI user, JSON-RPC client, shell binding,
> persisted event reader, model provider adapter, or tool caller rely on?

Everything else is implementation detail.

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

## Delete Policy

Delete the existing tests and helpers instead of migrating them.

Do not keep compatibility shims for old helper modules. The rewrite should
create a small new harness that matches the behavior boundaries below.

Do not carry over tests that only assert:

- private helper return shapes
- tuple vs list internal storage
- exact dataclass field normalization
- monkeypatch call paths
- private method dispatch
- line-by-line internal event dicts outside event contract tests
- defensive validation for internal Python call sites

If an old behavior still matters, rewrite the oracle:

| Old-style assertion | New behavior assertion |
|---|---|
| `params.tools == ("read", "bash")` | the persisted requested-turn event exposes the requested tools |
| `build_prompt_step()` returns a specific tuple | the model gateway receives the expected prompt behavior |
| `record_runtime_draft()` is called once | the event store contains one durable runtime event |
| private helper raises `ValueError` | the CLI/RPC boundary returns the documented user-facing error |
| exact internal tool result dict | the public timeline shows the tool result status and content |

## Target Layout

```text
tests/
  conftest.py

  shared/
    cli.py
    events.py
    models.py
    rpc.py
    shell.py
    tools.py
    trace.py

  zeta/
    conftest.py

    contracts/
      test_jsonrpc.py
      test_events.py
      test_event_store.py
      test_capabilities.py

    runtime/
      test_agent_loop.py
      test_dispatcher.py
      test_session_turn.py
      test_cancellation.py

    cli/
      test_server.py

    models/
      test_profiles.py
      test_chat_payloads.py
      test_chat_streaming.py
      test_responses_payloads.py
      test_responses_streaming.py
      test_codex_auth.py

    context/
      test_prompt_components.py
      test_prompt_builder.py
      test_project_context.py
      test_skills.py
      test_system_prompt.py
      test_compaction.py

    trace/
      test_store.py
      test_projection.py
      test_replay.py
      test_diff.py
      test_tools_view.py
      test_tree.py

    agents/
      test_spec.py
      test_manifest.py
      test_runtime_compile.py

  sigil/
    conftest.py

    cli/
      test_help.py
      test_ask_step_do.py
      test_events.py
      test_history.py
      test_install_doctor.py
      test_model.py
      test_session.py
      test_status.py
      test_trace.py
      test_operators.py

    workflows/
      test_handoff.py
      test_shell_resume.py
      test_rendering.py
      test_turn_records.py

    shell/
      test_binding.py
      test_glyphs.py
      test_recording.py
      test_interactive.py
      test_completion.py

    state/
      test_history_records.py
      test_failure_context.py
      test_sessions.py
      test_bundle.py
      test_spool.py

    display/
      test_terminal_digest.py
      test_transcript.py
      test_context_footer.py

    tools/
      test_read.py
      test_grep.py
      test_bash.py
      test_write.py
      test_edit.py
      test_ls.py
      test_web.py
      test_query_log.py
```

Keep the separation strict:

- Zeta tests should not import Sigil workflow modules unless the behavior being
  tested is explicitly the Sigil-to-Zeta integration.
- Sigil tests may exercise Zeta through public integration points, but should
  not assert Zeta internals.
- Shared helpers must stay behavior-oriented and small. If a helper is only used
  by one side, put it under that side's `conftest.py` or local helper module.

## Global Fixtures

### `tests/conftest.py`

Responsibilities:

- isolate `HOME`
- isolate `SIGIL_STATE_DIR`
- isolate `ZETA_STATE_DIR`
- clear `SIGIL_SESSION_ID`
- clear `SIGIL_SESSION_DIR`
- clear `ZETA_SESSION_ID` unless a test explicitly sets it
- prevent tests from reading or writing real developer state

Keep it small. Do not put domain-specific helpers here.

### `tests/zeta/conftest.py`

Responsibilities:

- create isolated Zeta runtime sessions
- create in-memory and SQLite event stores
- create in-memory and SQLite trace stores
- provide fake model gateways
- provide small capability registries

No Sigil CLI or shell helpers here.

### `tests/sigil/conftest.py`

Responsibilities:

- create isolated Sigil sessions
- provide CLI runner helpers for `sigil`
- provide temporary shell binding install roots
- provide history/state seeders

No direct Zeta runtime internals here unless the test is explicitly an
integration test.

## Shared Helpers

### `tests/shared/rpc.py`

Provide:

- `rpc_request(method, params=None, id=1)`
- `rpc_notification(method, params=None)`
- `rpc_messages(output)`
- `assert_rpc_result(message, id=1)`
- `assert_rpc_error(message, code, zeta_code, id=1)`

Used by:

- `tests/zeta/contracts/test_jsonrpc.py`
- `tests/zeta/cli/test_server.py`
- Sigil RPC integration tests only if they interact through JSON-RPC.

Rules:

- Own JSON-RPC envelope assertions.
- Do not expose protocol handler internals.

### `tests/shared/events.py`

Provide:

- `draft_event(type, payload=None, session_id=None, turn_id=None)`
- `event(type, payload=None, session_id=None, turn_id=None, cursor=None)`
- `append_events(store, *drafts)`
- `event_views(events)`
- `event_of_type(events, type)`
- `assert_event_subset(event, **fields)`

Rules:

- Exact event representation assertions belong in Zeta event contract tests.
- Other tests should assert the behavior-relevant subset.

### `tests/shared/models.py`

Provide:

- `FakeModelGateway`
- `text_response(content, reasoning=None)`
- `tool_call_response(name, arguments=None, call_id="call-1")`
- `streaming_response(chunks, final)`
- `chat_sse_lines(*payloads)`
- `responses_sse_frames(*payloads)`

Rules:

- Runtime behavior tests use fake gateways.
- Provider adapter tests use exact provider payload fixtures.

### `tests/shared/tools.py`

Provide:

- `registered_capability(name, provider="test", run=None, stage=None)`
- `tool_registry_with(*capabilities)`
- `recording_tool(result=None)`
- `mutating_tool(stage_result=None, direct_result=None)`

Rules:

- Assert observable invocations and results.
- Do not expose registry private maps.

### `tests/shared/cli.py`

Provide:

- `invoke_cli(args, *, env=None, input=None)`
- `assert_exit(result, code=0)`
- `json_stdout(result)`
- `plain_stdout(result)`
- `plain_stderr(result)`

Rules:

- Preserve stdout/stderr separation.
- Strip ANSI only when the test is not about terminal styling.

### `tests/shared/shell.py`

Provide:

- static zsh binding runner
- stub executable creation
- shell log parsing
- interactive PTY harness

Rules:

- Used by `tests/sigil/shell/` and shell-resume workflow tests only.

### `tests/shared/trace.py`

Provide:

- trace store builders
- object graph seeders
- short object ref helper

Rules:

- Trace graph tests can assert exact persisted graph behavior.
- Sigil trace CLI tests should assert user-visible output.

## Zeta Tests

### `tests/zeta/contracts/test_jsonrpc.py`

Pin JSON-RPC behavior as a client sees it.

Cases:

- `initialize` returns server metadata.
- unknown method returns method-not-found error.
- invalid JSON writes parse error.
- non-object JSON-RPC messages are rejected.
- notifications do not produce responses.
- `events.list` without configured reader returns server error.
- `events.list` returns events in cursor order.
- `events.list` filters by session and run.
- `events.list` handles invalid cursor values according to policy.
- `events.list` handles invalid limit values according to policy.
- `events.subscribe` returns subscription id and next cursor.
- published events are sent only to matching subscriptions.
- `events.publish` rejects runtime lifecycle ingress.
- `tools.register` registers RPC client tools.
- `tools.register` rejects duplicate client-owned tools.
- `tools.register` rejects non-`rpc` providers.
- `tools.respond` records successful, malformed, cancelled, and missing results.
- client tool calls emit `tools.call`.
- client tool calls time out.
- client disconnect returns failed tool result.
- `session.run` starts a run and returns lifecycle result.
- `session.cancel` cancels running task-backed runs.
- `session.cancel` handles unknown and completed runs.

Pinned observations:

- JSON-RPC envelope.
- method names.
- public error codes.
- public result fields.

Do not assert:

- internal protocol handler maps.
- dataclass field representation.
- private run-state storage.

### `tests/zeta/contracts/test_events.py`

Pin runtime event projection and public event views.

Cases:

- model event draft becomes durable model-call event.
- tool call draft becomes durable tool-call event.
- tool result draft becomes durable completed, failed, or refused event.
- user message draft preserves session and turn behavior.
- turn abort draft preserves reason.
- `event_view()` returns public timeline representation.
- idempotency keys produce stable event ids where promised.
- payload timeline type overrides durable event type.
- model usage event projects as model usage.

Pinned observations:

- public event representation.
- idempotency behavior.
- cursor stringification in public views.

### `tests/zeta/contracts/test_event_store.py`

Pin event store behavior.

Cases:

- memory and SQLite stores append events.
- SQLite assigns monotonic cursors.
- filters by session, turn, prefix, cursor, and limit.
- idempotent append returns existing event.
- payload snapshots are immutable from caller mutation.
- causality traversal follows `caused_by`.
- causality traversal stops on cycles.
- event store path respects isolated state dir.
- large events survive process boundaries.

Pinned observations:

- event ordering.
- cursor behavior.
- idempotency behavior.
- storage compatibility.

### `tests/zeta/contracts/test_capabilities.py`

Pin Zeta capability registry behavior.

Cases:

- duplicate canonical capability id rejected.
- ambiguous model-visible name rejected.
- qualified capability ids disambiguate.
- unknown capability returns failed result.
- executor exception becomes failed result.
- malformed capability result becomes failed result.
- stage mode stages mutating capability.
- direct mode executes mutating capability.
- projection exposes model-visible descriptor.

Pinned observations:

- model-visible projection representation.
- public result representation.
- stage/direct behavior.

### `tests/zeta/runtime/test_agent_loop.py`

Use fake model gateways and fake registries.

Cases:

- text-only model answer finalizes with `final_answer`.
- model reasoning is recorded on model event.
- model tool call emits tool call before invoking tool.
- read-only tool result feeds next model turn.
- multiple read-only tools run in order.
- streamed text emits runtime chunks and final answer is marked streamed.
- reasoning deltas emit runtime status updates.
- runtime UI events are not included in next prompt.
- disallowed tool is refused and not invoked.
- unknown tool is refused and not invoked.
- invalid JSON tool arguments are refused and not invoked.
- tool executor crash becomes failed tool result.
- staged mutating tool stops when configured to stop.
- direct mutating tool continues to next model turn.
- default max turns stops runaway loops.
- prompt trace exists for each model request.
- model telemetry attaches to first tool result and run result.

Pinned observations:

- public event types.
- tool invocation or non-invocation.
- final outcome.
- staged effect behavior.

Do not assert:

- step helper names.
- internal `RunState` layout.

### `tests/zeta/runtime/test_dispatcher.py`

Cases:

- unmatched event persists but routes nowhere.
- matching event creates work for matching agent.
- exact and prefix patterns match.
- duplicate events do not route twice.
- dispatcher rejects external lifecycle events.
- dispatcher rejects recursive agent publication.
- publication hop limit is enforced.
- failed agent work is recorded.
- matching agents can run concurrently.

Pinned observations:

- durable lifecycle events.
- routed agent ids.
- failure state.

### `tests/zeta/runtime/test_session_turn.py`

Cases:

- session-turn request creates a durable requested-turn behavior.
- session turn records user message.
- enabled tools are projected from requested tools.
- `ask` and `propose` run in stage mode.
- `do` runs in direct mode.
- explicit context is passed to the model prompt behavior.
- current timeline excludes current user event from prior timeline.
- session result includes trace refs when trace projection succeeds.
- session result degrades when trace data is missing.

Pinned observations:

- event/output boundary.
- public session result.

Do not assert:

- `SessionRunParams` internal field types.

### `tests/zeta/runtime/test_cancellation.py`

Cases:

- cancellation before model call aborts.
- cancellation while run is active publishes cancelled result.
- wall-clock budget aborts.
- cooperative cancellation keeps cooperative runner alive when intended.
- task cancellation cancels native async runner.

Pinned observations:

- public outcome.
- abort event reason.

### `tests/zeta/cli/test_server.py`

Cases:

- `zeta` console script is declared.
- stdio server handles `initialize`.
- stdio server reports structured errors.
- pure Zeta session run does not create Sigil turn records.

Pinned observations:

- process behavior.
- stdout/stderr or JSON-RPC output.

### `tests/zeta/models/test_profiles.py`

Cases:

- loads user config.
- reads thinking and API.
- rejects unknown thinking/API.
- rejects multiple defaults.
- configured default resolves without session selection.
- session selection beats default.
- vanished session profile falls back.
- builtin fallback works.
- context token metadata from `/props` and `/v1/models`.

Policy decision:

- decide whether malformed config types are rejected, preserved, or allowed to
  fail naturally. Pin only the chosen user-facing behavior.

### `tests/zeta/models/test_chat_payloads.py`

Cases:

- `ModelInput` renders chat completion request.
- request body leaves thinking to model by default.
- `thinking="none"` disables thinking.
- reasoning effort is sent when configured.
- native tools are sent.
- structured output sends JSON schema.
- invalid structured output schema is rejected.
- large max tokens default.
- model telemetry reported.

Pinned observations:

- provider request payload.

### `tests/zeta/models/test_chat_streaming.py`

Cases:

- stream forwards content deltas in order.
- stream forwards reasoning deltas.
- usage chunk preserved.
- tool call fragments reconstructed.
- multiple tool calls ordered by index.
- malformed stream events rejected.
- stream closes on error.
- HTTP error detail from JSON and plain bodies.
- missing content type accepted if stream is valid.
- truncated tool calls rejected.
- text cut by max tokens accepted according to policy.

### `tests/zeta/models/test_responses_payloads.py`

Cases:

- system prompt moves to instructions.
- assistant and tool messages convert correctly.
- recorded replay items replay verbatim.
- tools convert with `strict`.
- thinking maps to reasoning effort.
- session cache key carried.
- Codex URL and headers.
- Codex requires model name.
- structured output.

### `tests/zeta/models/test_responses_streaming.py`

Cases:

- text and reasoning accumulate.
- tool calls collected.
- incomplete response marked length.
- error events raise.
- quota errors render reset hint.
- terminal event required.

### `tests/zeta/models/test_codex_auth.py`

Cases:

- fresh credentials load without refresh.
- account ID read from tokens.
- expired token refreshes and writes back.
- missing file reports login instruction.
- unreadable file rejected.
- refresh failure reported.
- concurrent refresh rereads after lock.

### `tests/zeta/context/test_prompt_components.py`

Cases:

- user message boundary round trip.
- assistant message boundary round trip.
- tool call boundary round trip.
- tool result boundary round trip.
- representation and token cost.
- prefix order.
- source events retained.

### `tests/zeta/context/test_prompt_builder.py`

Cases:

- noop transform matches chat messages.
- prompt links components.
- request reconstructs and verifies.
- pure plan is repeatable.
- commit object ID is idempotent.
- render model input matches prepared prompt.
- model output projects from event.
- reconstruction flags changed component.
- prompt object stores payload hash, not payload.

### `tests/zeta/context/test_project_context.py`

Cases:

- global-to-local context loading.
- exact `AGENTS.md` filename required.
- missing global directory ignored.
- oversized files capped.
- total cap drops broadest first.

### `tests/zeta/context/test_skills.py`

Cases:

- user and project skills discovered.
- collision precedence.
- duplicate canonical paths.
- invalid metadata reported.
- leading directive expansion.
- inline skill expansion.
- unknown skills unchanged.

### `tests/zeta/context/test_system_prompt.py`

Cases:

- does not advertise skills incorrectly.
- product-neutral and dynamic.
- states today's date.
- points at query log for older history.

### `tests/zeta/context/test_compaction.py`

Cases:

- structural trim compacts old bulky read/grep results.
- non-read/grep results skipped.
- default trim is late safety valve.
- current tool results preserved.
- works without trace IDs.
- task state replaces transcript.
- task state fails open.
- task state extraction input omits duplicates.
- extraction cached per source set.
- budget thresholds escalate.
- drop-oldest removes historical messages until under budget.
- drop-oldest drops tool results with their calls.
- trim env modes build ladders.
- unknown trim mode warns.

### `tests/zeta/trace/test_store.py`

Cases:

- object IDs ignore dict key order.
- object IDs change for schema/data/links.
- SQLite persists objects, refs, derivations, closure.
- incompatible schema reported.
- read-only store rejects writes.
- read-only other-session open.
- search matches data and filters kind.
- wildcard characters treated literally.
- derivations forward and backward queries.
- derivation IDs content-scoped across sessions.
- object listing newest first.
- filter by multiple kinds.

### `tests/zeta/trace/test_projection.py`

Cases:

- durable events project trace objects.
- tool result is durable.
- tool call caused by assistant event.
- model/tool drafts keep trace links out of public payload.
- fresh session timeline projects from durable log.
- timeline rehydrates assistant content and reasoning from graph.
- untraced assistant content remains inline.
- orphan tool result rendering strips trace fields.
- truncated tool call arguments repaired for transcript where intended.

### `tests/zeta/trace/test_replay.py`

Cases:

- replay records traced answer.
- replay diffs old and new prompt.
- replay renders tool call answers.
- replay honors named profile.

### `tests/zeta/trace/test_diff.py`

Cases:

- component changes reported.
- stat output one line per change.
- prompt object requirement enforced.

### `tests/zeta/trace/test_tools_view.py`

Cases:

- tool calls and results joined in JSON.
- failed filter recovers content and bash errors.
- plain output uses uniform error.
- successful filter.
- status filter conflicts.
- all-sessions sort by trace time.

### `tests/zeta/trace/test_tree.py`

Cases:

- producer walk by default.
- consumer walk with down.
- depth respected.

### `tests/zeta/agents/test_spec.py`

Cases:

- loads frontmatter, body, slug, and content hash.
- renders prompt.
- event matching.
- return schema derivation.
- schedule subset behavior.

### `tests/zeta/agents/test_manifest.py`

Cases:

- unknown tool rejected.
- unknown event rejected.
- unknown extension rejected.
- extension policy follows current product decision.

### `tests/zeta/agents/test_runtime_compile.py`

Cases:

- spec compiles to event dispatch agent.
- prompt validation rejects unknown root.

## Sigil Tests

### `tests/sigil/cli/test_help.py`

Cases:

- top-level help lists public commands.
- command help includes required examples or exit-contract text.
- no-command invocation shows help.
- lazy commands resolve.
- lazy import boundaries for expensive workflow/display/model modules.

Pinned observations:

- command names.
- documented exit-contract phrases.

### `tests/sigil/cli/test_ask_step_do.py`

Cases:

- `ask` accepts piped input.
- `ask` opens editor when no question and no stdin.
- `ask` aborts on empty editor save.
- `step` writes handoff file.
- `step` keeps trace off stdout.
- `step` supplies workflow persona.
- `do` executes directly.
- model unavailable returns documented failure.
- stdout stays clean for pipes.

### `tests/sigil/cli/test_events.py`

Cases:

- events list defaults to recent events.
- filters by session.
- causality subcommands.
- raw JSON validation.
- failure recorded label is not prefixed as glyph.

### `tests/sigil/cli/test_history.py`

Cases:

- log lists sessions newest first.
- log filters workflow, failed, session, and touched path.
- log JSON output behavior.
- log show renders full record.
- log show handles ambiguous and unknown IDs.
- blame lists turns touching a file.
- blame reports untouched files.

### `tests/sigil/cli/test_install_doctor.py`

Cases:

- zsh binding install copies binding and updates rc idempotently.
- glyph aliases can be disabled.
- runtime bins are resolved.
- JSON install output reports paths.
- doctor reports expected checks.
- doctor endpoint checks.
- doctor Codex auth checks.

### `tests/sigil/cli/test_model.py`

Cases:

- model switch stores active profile per session.
- unknown profile rejected.
- list resolves URLs and marks active profile.
- show reports source.
- default profile visible when active.

### `tests/sigil/cli/test_session.py`

Cases:

- session list includes last event context.
- session rename stores display name.
- blank rename rejected.
- transcript renders conversation.
- transcript limit and JSON output.
- empty session message.
- traversal-safe session IDs.

### `tests/sigil/cli/test_status.py`

Cases:

- clean status with no live state.
- model line reports session selection and stale profile.
- last failure reported.
- pending staged handoff reported.
- JSON output includes model and history behavior.

### `tests/sigil/cli/test_trace.py`

Cases:

- trace reinit recreates unified database.
- trace log default narrative kinds.
- trace log filters by kind/all/session.
- trace grep lists matches and no-match output.
- trace diff reports component changes.
- trace replay records traced answer.
- trace tools joins calls and results.
- trace tools filters failed/success/status.
- trace tree walks producers and consumers.
- trace show renders humans first.
- ref resolution handles refs, prefixes, ambiguity, and unknown IDs.

### `tests/sigil/cli/test_operators.py`

Cases:

- command verb is not registered.
- ask verb accepts piped input.
- ask/step editor behavior.
- step continue behavior.
- main rewrites model runtime error behavior.

### `tests/sigil/workflows/test_handoff.py`

Cases:

- staged bash handoff created.
- shell result appends tool result.
- whitespace-edited staged command still matches when intended.
- unrelated command cancels or ignores staged handoff according to policy.
- intervening shell turns are included.
- resolved handoff is not reused.
- cancelled handoff emits cancelled effect.

### `tests/sigil/workflows/test_shell_resume.py`

Cases:

- staged command writes resume file.
- unrelated command leaves resume file untouched.
- interrupted command skips resume.
- auto-continue opt-out.

### `tests/sigil/workflows/test_rendering.py`

Cases:

- final answer separated from trace.
- context usage renders on trace stream.
- context usage renders after buffered answer.
- context usage remains at bottom after tools.
- tool start prints while agent runs.
- streamed text appears before tool trace.
- no duplicate final answer after streaming.
- no extra blank lines between tool calls.

### `tests/sigil/workflows/test_turn_records.py`

Cases:

- answered turn behavior.
- staged turn behavior.
- executed turn behavior.
- failed turn behavior.
- aborted turn behavior.
- effect record behavior.
- optional blocks omitted when absent.
- model telemetry included when present.
- prompt trace refs included when present.

Pinned observations:

- persisted turn/effect record representation.

Do not assert:

- recorder method call order.

### `tests/sigil/shell/test_binding.py`

Cases:

- wrappers call current CLI behavior.
- zeta step wrapper calls zeta handoff directly.
- binding functions survive hostile user options.
- dispatch word falls back without UTF-8 locale.

### `tests/sigil/shell/test_glyphs.py`

Cases:

- comma dispatch routes to ask.
- plus dispatch routes to run.
- status glyph routes to status.
- glyph split preserves raw text and multiline buffers.
- glyph widget rewrites buffer to safe dispatch line.
- question-mark globbing preserved.

### `tests/sigil/shell/test_recording.py`

Cases:

- shell turns recorded without handoff.
- leading space skips recording.
- sigil commands skip recording.
- unsupported caret text recorded.
- opt-out disables recording.
- two PTYs get distinct sessions.
- same TTY keeps session.

### `tests/sigil/shell/test_interactive.py`

Cases:

- interactive comma dispatch.
- interactive plus dispatch with pipeline.
- quoted prompts preserve shell semantics.
- glyph line recall behavior.
- display decoration does not leak.
- ctrl-c and job-control behavior.
- autosuggestions and syntax-highlighting compatibility.

### `tests/sigil/shell/test_completion.py`

Cases:

- plus completion registered after `compinit`.
- plus completes like underlying command.

### `tests/sigil/state/test_history_records.py`

Cases:

- turn record writes durable event.
- effect record writes durable tool event.
- latest turn record per ID.
- history ignores non-history events.
- query filters workflow, outcome, since, session, touched path.
- turn ID prefix matching.
- pending staged command clears on resolution.
- cost since sums session turns.

### `tests/sigil/state/test_failure_context.py`

Cases:

- failure prompt uses recorded failure without inventing output.
- common failure fixtures.
- snippets and safe context.
- redaction before storage.
- unrelated question attaches active failure context.
- successful turn clears active failure context.
- already-seen failure omitted.

### `tests/sigil/state/test_sessions.py`

Cases:

- recent turns missing file returns empty.
- recent turns returns last N in order.
- malformed lines skipped.
- session clear removes continuity.
- session dir traversal blocked.
- plain session ID preserved.
- unsafe session ID maps deterministically.

### `tests/sigil/state/test_bundle.py`

Cases:

- export collects turns, effects, and trace closure.
- export honors since and session filters.
- skips sessions without trace stores.
- import restores history and trace queries.
- import idempotent.
- event causality preserved.

### `tests/sigil/state/test_spool.py`

Cases:

- spool ingestion records command with spool time.
- removes spool after ingest.
- malformed records skipped.
- failure fanout.
- missing spool noop.
- orphaned claims recovered.
- fresh claims left alone.
- CLI invocation ingests spool.

### `tests/sigil/display/test_terminal_digest.py`

Cases:

- summarizes tool results.
- classifies progress events.
- keeps short turns compact.
- quotes web search query.
- no empty status detail.
- switches to chapters.
- bounds repeated chapter lines.
- quiet mode keeps failures and final digest.
- final receipt summarizes effects.
- reasoning phase behavior.

### `tests/sigil/display/test_transcript.py`

Cases:

- conversation blocks.
- tool calls joined to results.
- failed results marked.
- redundant failure prefix stripped.
- tool exchanges contiguous.
- unmatched results standalone.
- embedded tool calls.
- noise and empty events skipped.
- reasoning before answer.
- user scaffolding dimmed.

### `tests/sigil/display/test_context_footer.py`

Cases:

- context estimate summary.
- total token summary.
- estimates tool result tokens.
- telemetry replaces stale estimates.
- TTY footer ephemeral.
- non-TTY footer final only.

### `tests/sigil/tools/test_read.py`

Cases:

- file read behavior.
- offset and limit behavior.
- limit past end behavior.
- binary file rejection.
- returned character cap.
- public URL fetch if retained.

### `tests/sigil/tools/test_grep.py`

Cases:

- metadata guides model tool choice.
- total limited metadata.
- content truncation.
- fallback without ripgrep.
- fallback stops at limit.
- invalid pattern error.
- tagged matches ground hashline edit.
- ast-grep metadata and tagged structural matches.

### `tests/sigil/tools/test_bash.py`

Cases:

- staged command effect.
- direct execution.
- failure error normalization.
- invalid UTF-8 replacement.
- timeout kills command.
- large output truncation.
- duration metadata.

### `tests/sigil/tools/test_write.py`

Cases:

- direct write.
- staged hash metadata.
- direct hash metadata.
- before hash omitted for new file.

### `tests/sigil/tools/test_edit.py`

Cases:

- writes patch artifact.
- exact replacement.
- hashline swap from read tag.
- hashline insert/delete direct.
- stale tag rejection.
- malformed hashline rejection.
- no-op rejection.
- direct replace.
- non-UTF-8 rejection.
- write failure.
- ambiguous exact replacement.
- no-newline marker.
- direct and staged hashes.

### `tests/sigil/tools/test_ls.py`

Cases:

- directory listing.
- large-file filtering without shelling out.

### `tests/sigil/tools/test_web.py`

Cases:

- web search schema matches Codex behavior.
- missing Codex credentials reported.
- Codex payload posted.

### `tests/sigil/tools/test_query_log.py`

Cases:

- all sessions listed with cited IDs.
- current session filter.
- filters and limit cap.
- one turn expanded by prefix.
- bad IDs and bad since reported.
- empty history reported.
- readonly ask builtin.

## Validation Policy Decisions

Decide these once before writing tests. Then pin the decision only at the
relevant behavior boundary.

| Question | Boundary | Default recommendation |
|---|---|---|
| Should malformed JSON-RPC params return structured errors? | Zeta RPC | Yes |
| Should internal Python call sites validate argument types? | Internal | No |
| Should `session.run.objective` require a string? | Zeta RPC/session | Yes, unless raw JSON values are intentionally accepted |
| Should `events.list.after` require a string cursor? | Zeta RPC | Yes |
| Should `events.list.limit` require an integer? | Zeta RPC | Yes |
| Should client tools require schemas? | Zeta RPC/tools | No, unless model provider behavior needs strict schemas |
| Should capability schemas be validated at registration? | Zeta capabilities | No, unless registration is a public config boundary |
| Should tool-call args be JSON-schema validated before invocation? | Zeta runtime/tools | No, unless this is a product safety policy |
| Should authored specs reject wrong field types? | Zeta agents | Yes for user-authored files, but keep checks semantic and minimal |
| Should model profile TOML reject wrong field types? | Zeta models | Yes for user-authored config, but avoid defensive internal validation |
| Should old persisted events stay readable? | Zeta/Sigil storage | Yes |

## Build Order

This is the order to build the replacement suite from zero.

### Step 1: Zeta contracts

Create:

- `tests/conftest.py`
- `tests/shared/events.py`
- `tests/shared/rpc.py`
- `tests/zeta/conftest.py`
- `tests/zeta/contracts/test_events.py`
- `tests/zeta/contracts/test_event_store.py`
- `tests/zeta/contracts/test_jsonrpc.py`
- `tests/zeta/contracts/test_capabilities.py`

Run:

```bash
uv run pytest tests/zeta/contracts -q
```

### Step 2: Zeta runtime

Create:

- `tests/shared/models.py`
- `tests/shared/tools.py`
- `tests/zeta/runtime/test_agent_loop.py`
- `tests/zeta/runtime/test_dispatcher.py`
- `tests/zeta/runtime/test_session_turn.py`
- `tests/zeta/runtime/test_cancellation.py`

Run:

```bash
uv run pytest tests/zeta/contracts tests/zeta/runtime -q
```

### Step 3: Zeta models, context, trace, agents

Create:

- `tests/zeta/models/*.py`
- `tests/zeta/context/*.py`
- `tests/zeta/trace/*.py`
- `tests/zeta/agents/*.py`
- `tests/zeta/cli/test_server.py`

Run:

```bash
uv run pytest tests/zeta -q
```

### Step 4: Sigil tools and state

Create:

- `tests/sigil/conftest.py`
- `tests/sigil/tools/*.py`
- `tests/sigil/state/*.py`

Run:

```bash
uv run pytest tests/sigil/tools tests/sigil/state -q
```

### Step 5: Sigil CLI, workflows, display

Create:

- `tests/shared/cli.py`
- `tests/sigil/cli/*.py`
- `tests/sigil/workflows/*.py`
- `tests/sigil/display/*.py`

Run:

```bash
uv run pytest tests/sigil/cli tests/sigil/workflows tests/sigil/display -q
```

### Step 6: Sigil shell

Create:

- `tests/shared/shell.py`
- `tests/sigil/shell/*.py`

Run:

```bash
uv run pytest tests/sigil/shell -q
```

### Step 7: Full suite

Run:

```bash
uv run pytest -q
uv run ruff check
uv run ty check
uvx --with radon radon cc src tests -s
```

Run `pre-commit` only when docs or config changed:

```bash
uv run pre-commit run --all
```

## Coverage Targets

The goal is contract coverage, not line coverage.

Minimum acceptable coverage:

- every public Zeta JSON-RPC method has success and failure behavior tests.
- Zeta event store behavior is pinned for memory and SQLite stores.
- Zeta agent loop has text, tool, staged, direct, error, stream, telemetry, and
  abort paths.
- Zeta model provider adapters have exact request payload and stream
  reconstruction tests.
- Zeta trace replay can reconstruct at least one full model/tool exchange.
- Sigil CLI has at least one behavior test per public command group.
- Sigil shell has static binding tests and critical interactive glyph tests.
- Sigil builtin tools have side-effect tests for direct and staged behavior.
- Sigil history and bundle records round trip.

## Anti-Goals

Do not rebuild:

- a mirror of the current test file layout.
- a giant shared helper module.
- broad monkeypatch-heavy tests of private helpers.
- tests that exist only to make coverage numbers high.
- compatibility tests for behavior intentionally removed.
- validation tests for internal call paths.

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
