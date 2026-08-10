# Phase 0 report — proving the IPC boundary

Everything in the Phase 0 brief is done, with two mid-flight changes
Remi requested while the work was in progress: the `transport`
configuration flag was removed (IPC is the only transport for
connectors that have a wire-v0 child), and connectors now auto-enable
from the registry (the `agents/connectors.yaml` enable list is gone).
The commits, in order:

1. `Mint content addresses with domain-separated blake3`
2. `Specify wire-v0 and its conformance vectors`
3. `Implement wire-v0 in zeta.wire`
4. `Run the filesystem connector as a wire-v0 subprocess`
5. `Mint new identifiers as b3 addresses, dual-read the sha256 epoch`
6. this report

## What exists now

- `spec/wire-v0.md` — the normative protocol: ndjson child-stdio
  framing, versioned envelopes, `hello`/`hello_ack` handshake with the
  full `role` definition and the reserved `effects_are_proposals`
  capability, ack-window flow control, heartbeat liveness, shutdown
  escalation, enumerated error codes, `b3:` address domains with the
  legacy dual-read rule.
- `spec/vectors/` — golden vectors consumed directly by the test
  suite: 11 valid envelopes (byte-exact canonical form), 21 invalid
  envelopes with rule-token `.reason.txt` files, a scripted handshake
  session with `_dir` direction markers, and 20 address vectors
  covering all five domains.
- `zeta/src/zeta/wire/` — `envelopes` (hand-rolled validation whose
  rule tokens match the vector reason files), `framing` (junk-tolerant
  buffered reader), `plugin.run_source` (child SDK: handshake,
  deterministic event ids, ack window, heartbeats, shutdown, stdout
  redirected so prints/logging land on stderr), `host.SubprocessSource`
  (supervision: handshake timeout, heartbeat monitoring, protocol
  strikes, capped exponential respawn backoff, SIGTERM→SIGKILL
  escalation), `fs_inbox` (the filesystem connector as a child).
- The demo's fs connector runs as a supervised subprocess wired
  through `harness/connector_bridge.py`; payloads, event types, and
  idempotency keys are identical to the in-process path (golden
  comparison test).
- `zeta/src/zeta/addresses.py` — the single minting point for `b3:`
  addresses with the five frozen `derive_key` contexts, plus
  `is_legacy`/`is_b3`.

## Recon findings and corrections to the brief's §1

- **Event ids are random, not hash-derived.** `Event.from_draft`
  assigns `evt_<uuid4hex>` (`zeta/src/zeta/events.py`). The
  truncated-sha256 (24-hex) ids in `ids.py` are the `pub_`/`wait_`
  *handles* for publish/wait requests. Those handles are what moved to
  `b3:` chain-domain addresses; journal event ids are untouched (the
  journal schema is out of scope).
- **`provenance.py` checks prompt ids; it does not mint them.** The
  mint is `Object.content_address()` in `substrate/objects.py`, which
  addresses every prompt-trace object. That mint moved to the prompt
  domain; `provenance.py` and `substrate/store.py` dual-read.
- **`skill.sha256` is minted in `authoring/resources.py`**
  (`load_skill_registry`); `harness/project.py` records and verifies
  it. The mint moved to the skill domain; verification dual-reads by
  epoch.
- **Connectors are a first-class package**, not just entry points:
  `src/connectors/` defines `EventConnector`/`EventConnectorRegistry`
  with polled ingress, push ingress, and egress; `zeta serve` polls
  ingress bindings once per second via
  `harness/connector_bridge.run_ingress_forever`, and events land via
  `RuntimeEventStore.accept(DraftEvent)` →
  `EventJournal.append_in_transaction` (idempotency-key `ON CONFLICT
  DO NOTHING`).
- The event path an agent sees: connector `DraftEvent` → payload
  validated against the project event registry → idempotency key
  rendered from the binding template (`file:{path}` in the demo) →
  journal insert → routing → queue item → attempt → run.
- Tests: single flat suite under `zeta/tests/`, pytest with
  `asyncio_mode=auto`, run from the repo root; CI runs pre-commit
  (ruff check/format, ty, vulture, complexipy ≤ 25) and pytest with
  coverage ≥ 85% on Python 3.11–3.13.

## Discretionary decisions

- **Envelope conformance is keyed by rule tokens.** Each invalid
  vector's `.reason.txt` line 1 is a stable token
  (`payload_choice`, `bad_timestamp`, …) that `EnvelopeError.rule`
  reports; third-party SDKs map them however they like.
- **Event-id minting rule (spec §6.1).** An event id SHOULD be the
  event-domain address of the canonical JSON of
  `{"payload": …, "type": …}`; uniqueness among unacked events is the
  hard requirement. This preserves "same inputs, same id" for
  deterministic sources.
- **Schema refs are `name@major`** (`file.created@1`); the spec notes
  they become content addresses in a later version.
- **One child per ingress binding.** Simplest supervision and
  idempotency mapping; the demo has exactly one binding. A
  multi-watch child config exists (`fs_inbox` accepts a watch list)
  if consolidation is wanted later.
- **`payload_hash` events are refused with error code `unsupported`**
  (v0 defines no blob transfer; payload storage stays inline this
  phase). `run_source` raises on >64 KiB payloads so a plugin author
  finds out immediately.
- **Unacked-invalid events**: an event that fails payload-schema
  validation gets an `error` envelope and no ack (acking would claim
  durable acceptance). Persistent offenders eventually stall on their
  own window.
- **Watermark semantics on restart** match the in-process behavior:
  a respawned child starts a fresh watermark, so files that appear
  while the child is down are missed — exactly as an in-process
  `zeta serve` restart behaves today. Idempotency keys make
  redelivery safe.
- **Derivation ids** keep their `derivation:` prefix with the digest
  now taken from the prompt-domain blake3 (they are internal dedup
  keys, never dual-read).
- **Import boundaries evolved, not bypassed**: `addresses.py` joined
  `ids.py`/`paths.py` as a named leaf; leaves and `substrate` may now
  import `blake3` and each other. The layering intent (no upward
  imports) is unchanged, and `wire` was added to the
  forbidden-dependencies map as a low-level layer.
- **A failing entry-point connector is skipped with a warning** —
  required by auto-enablement, since pushover/telegram/slack
  hard-fail construction without credentials and must not poison
  unrelated projects.

## Deviations from the brief

- **`transport: ipc | inproc` flag: removed** at Remi's request
  mid-phase. IPC is unconditional for connectors with a wire-v0 child
  (only `filesystem` this phase); the in-process polled path survives
  only for connectors without a child (Slack). Rollback lever is
  `git revert`, not config.
- **`agents/connectors.yaml`: removed entirely** at Remi's request.
  Installation is enablement; `zeta serve --connectors` remains the
  process-level allowlist. `zeta new` no longer writes the file;
  existing projects' files are ignored.
- **Test-side edits.** The brief said "without breaking a single
  existing test"; the suite is green, but a handful of existing tests
  were *edited* where they pinned behavior this phase deliberately
  changed: the scaffold test (no `connectors.yaml`), the connector
  enable-list tests (auto-enable), one trace-CLI assertion that
  hardcoded the `sha256:` prefix of a *newly minted* object id, one
  helper in `zeta_test_support.py` (accepts both address epochs), and
  the import-boundary test (leaf/`wire` registration, as above).
- **Not switched to `b3:`** (out of the five frozen domains, or
  legacy-adjacent): journal `evt_` ids (random, journal schema frozen),
  `spec.sha256` (agent markdown hash), `harness/project.content_id`
  (generation ids, `project:sha256:` — self-verifying manifest hashes
  whose migration needs its own dual-read pass),
  `capabilities/delivery.content_hash`, `effects.py` effect keys, and
  the context payload/integrity hashes. All are recorded here as
  follow-up candidates; every one of them still resolves and verifies.

## Known gaps

- No runtime code mints blob-domain addresses yet (payloads stay
  inline until the Phase 2 blob store); the domain is specified and
  vectored.
- The IPC ingress task builds its child list from the project
  snapshot at `zeta serve` startup; binding changes on disk need a
  serve restart (the in-process poller re-reads every poll).
- `import_derivation` on a pre-b3 store can produce a duplicate
  derivation row (old sha256-keyed row plus a new b3-keyed one) for
  the same logical derivation.
- Auto-enablement means every `zeta serve` with a push-capable
  connector installed (slack/telegram, when their tokens are set)
  binds the push-ingress port; previously only projects that enabled
  them did.
- Local-only environment note: the repo's editable build compiles the
  Rust TUI, and the local default rustc (1.85) is older than its
  dependencies require (≥ 1.88), so `uv sync` fails locally;
  everything here ran via the existing venv with `uv run --no-sync`.
  CI runners are unaffected. Pre-existing, not introduced by this
  phase.

## Verifying the exit criteria

All commands from the repo root (drop `--no-sync` when the local
rustc issue is fixed):

```sh
# 1. Spec and vectors exist and the suite consumes them directly
ls spec/wire-v0.md spec/vectors/README.md
uv run --no-sync pytest zeta/tests/test_zeta_wire_vectors.py -q

# 2. Protocol implementation, fault drills, stdout purity, ack window
uv run --no-sync pytest zeta/tests/test_zeta_wire_plugin.py zeta/tests/test_zeta_wire_host.py -q

# 3. fs connector over IPC: child conformance, golden journal
#    comparison vs the in-process path, dedupe, containment
uv run --no-sync pytest zeta/tests/test_zeta_fs_ipc.py -q

# 4. Addresses: vector match, determinism, legacy dual-read
uv run --no-sync pytest zeta/tests/test_zeta_addresses.py -q

# 5. Entire suite (1111 passed, 2 skipped at time of writing)
uv run --no-sync pytest -q

# 6. Demo smoke (needs Codex credentials; verified end-to-end on
#    2026-08-10 — summary produced, one fs_inbox child process
#    observed, clean shutdown on SIGTERM):
uv run --no-sync zeta new /tmp/zeta-demo
cd /tmp/zeta-demo && uv run --no-sync zeta serve &   # from the repo venv
echo 'Buy milk.' > /tmp/zeta-demo/inbox/todo.txt
sleep 60 && cat /tmp/zeta-demo/summaries/todo.txt.md
kill %1
```

The demo's LLM step cannot run in CI (no model credentials there);
the ingress-to-journal half is covered by
`test_zeta_fs_ipc.py::test_ipc_ingress_reaches_the_journal_identically_to_inproc`,
and the agent-execution half by the existing worker tests with model
stubs.
