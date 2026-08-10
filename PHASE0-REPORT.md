# Phase 0 report — proving the IPC boundary

Phase 0 grew twice while in flight, both times at Remi's direction:
first the transport flag and the connectors.yaml enable list were
removed, then the whole connector subsystem was cut over — spec
amendments (config/secrets, crash-only reconnection, calls), a
sibling `zeta-connectors` package, shell-based discovery, and
deletion of every legacy path (entry points, in-process polled
ingress, the push-ingress HTTP server, webhooks). The commits, in
order:

1. `Mint content addresses with domain-separated blake3`
2. `Specify wire-v0 and its conformance vectors`
3. `Implement wire-v0 in zeta.wire`
4. `Run the filesystem connector as a wire-v0 subprocess`
5. `Mint new identifiers as b3 addresses, dual-read the sha256 epoch`
6. `Amend wire-v0 while it is soft: config, calls, crash-only doctrine`
7. `Teach the SDK and supervisor config, ack cursors, and calls`
8. `Connectors become wire-v0 executables; legacy paths deleted`
9. this report

## What exists now

- **`spec/wire-v0.md`** — the normative protocol: ndjson child-stdio
  framing, versioned envelopes, `hello`/`hello_ack` handshake (with
  `operations` declarations and runtime-composed `config`),
  ack-window flow control with ack-gated upstream cursors, heartbeat
  liveness, crash-only reconnection doctrine, shutdown escalation,
  `call`/`call_result` operations with per-operation delivery
  semantics, enumerated error codes, `b3:` address domains with
  legacy sha256 dual-read, and §13: connector executables with
  `--describe` manifests, discovered the shell's way.
- **`spec/vectors/`** — golden vectors the suite consumes directly:
  16 valid envelopes (byte-exact canonical form), 25 annotated
  invalid envelopes with stable rule tokens, two scripted handshake
  sessions (source flow; operations flow with a call exchange), and
  20 address vectors covering all five blake3 domains.
- **`zeta/src/zeta/wire/`** — both protocol sides. `plugin.run_source`
  is the child SDK (~20 lines for an author): handshake, config
  delivery, deterministic `b3:` event ids, ack window, `on_ack`
  cursor callbacks, operation serving, heartbeats, stdout redirected
  so prints and logging land on stderr. `host.SubprocessSource`
  supervises: handshake timeout, heartbeat monitoring, protocol
  strikes for junk, capped exponential respawn backoff,
  SIGTERM→SIGKILL escalation, and `call()` with pending futures that
  fail retryably when a child dies.
- **`zeta-connectors/`** — sibling workspace package; installing it
  puts `zeta-connector-{filesystem,slack,telegram,pushover}` on
  PATH, which is all the registration a runtime needs
  (`zeta-connectors[slack]` pulls the websocket extra). Filesystem
  polls watches from its bindings config; Slack ingests over Socket
  Mode and posts via a `connector_deduplicated` operation; Telegram
  long-polls `getUpdates` — no webhook, no public endpoint —
  advancing its offset only on runtime acks; Pushover is pure
  operations. All are crash-only: losing an upstream connection
  exits the child.
- **The runtime** discovers connectors by probing `zeta-connector-*`
  on PATH (plus the interpreter's own script directory, so absolute-
  path invocations of `zeta` still find sibling scripts) and
  executable files under `agents/connectors/`; each is described
  once (`--describe`, cached by mtime), validated, and recorded in
  project snapshots. Ingress spawns one child per **wave** — at most
  one binding per event type per child, so Telegram's two event
  types share one upstream cursor while two watched directories get
  two children. Egress runs as `call` envelopes against declared
  operations via one lazy operations child per connector, with the
  effect lifecycle events (`runtime.effect.*`, `runtime.egress.*`)
  unchanged.
- **`zeta/src/zeta/addresses.py`** — the single minting point for
  `b3:` addresses (five frozen `derive_key` contexts), with
  `is_legacy`/`is_b3`; new mints switched for publish/wait handles
  (chain), skill bodies (skill), and prompt-trace objects (prompt),
  dual-reading the sha256 epoch everywhere those ids are compared or
  resolved.

## Recon findings and corrections to the original brief's §1

- **Event ids are random, not hash-derived**: `Event.from_draft`
  assigns `evt_<uuid4hex>`; the truncated-sha256 ids in `ids.py` are
  the `pub_`/`wait_` *handles*. Those moved to `b3:`; journal event
  ids are untouched (journal schema frozen).
- **`provenance.py` checks prompt ids; the mint** is
  `Object.content_address()` in `substrate/objects.py` — that moved
  to the prompt domain, with dual-read in `provenance.py` and
  `substrate/store.py`.
- **`skill.sha256` is minted in `authoring/resources.py`**;
  `harness/project.py` records and verifies it — verification now
  dual-reads by epoch.
- Connectors were a first-class handler package (`src/connectors/`)
  polled in-process once per second, with a push-ingress HTTP server
  for webhooks — the planning map's "entry points only" was an
  under-description. All of that is now gone; `src/connectors/`
  survives as the vocabulary layer (bindings, `ConnectorManifest`,
  registry).
- Tests: flat suite under `zeta/tests/`, pytest `asyncio_mode=auto`;
  CI runs ruff/ty/vulture/complexipy and pytest with coverage ≥ 85%
  on Python 3.11–3.13 (currently 87%).

## Discretionary decisions

- Envelope conformance is keyed by stable rule tokens
  (`.reason.txt` line 1 = `EnvelopeError.rule`).
- Event ids SHOULD be the event-domain address of canonical
  `{"payload": …, "type": …}` (uniqueness among unacked events is
  the hard rule) — deterministic sources mint the same id every run.
- Schema refs are `name@major` (`file.created@1`); the spec notes
  they become content addresses later.
- Call payloads carry `{"payload": …, "options": …}` — the event
  payload plus the binding's options — and the binding's rendered
  idempotency key as `effect_key`.
- An event failing payload-schema validation gets an `error`
  envelope and **no ack** (an ack claims durable acceptance).
- A connector whose `--describe` fails is skipped with a warning —
  one broken or unconfigured executable must not poison unrelated
  projects. Delivery-credential checks happen at run time, never at
  describe time.
- Snapshot manifests record the describe document, not the spawn
  command (machine-local paths stay out of generation identity) and
  not handler source hashes (there are no handlers).
- Telegram's old webhook rationale ("a confirmed getUpdates offset
  is forgotten, leaving a loss window") is solved by the wire ack:
  the child confirms offsets only up to the acked prefix
  (`OffsetLedger`), so long-polling is now loss-free and the public
  endpoint requirement disappears.
- Derivation ids keep their `derivation:` prefix over the
  prompt-domain digest; import-boundary tests evolved (leaves may
  import blake3 and each other; `wire` registered as a layer) rather
  than being bypassed.

## Deviations from the original Phase 0 brief (all Remi-directed)

- No `connectors.transport` flag and no `inproc` rollback lever —
  IPC is the only transport; rollback is `git revert`.
- No `agents/connectors.yaml` — installation/PATH is enablement;
  `zeta serve --connectors` remains the process allowlist, and an
  allowlist that hides a connector the project binds fails loudly at
  validation.
- The brief said Slack "stays entry-point-loaded this phase" — it
  was instead converted (Socket Mode) as a spec-stressing
  instrument, and entry points were deleted rather than migrated.
- Webhook push ingress (and the HTTP server, Slack signature
  verification, `TELEGRAM_WEBHOOK_SECRET`) removed outright in favor
  of connector-owned transports.
- Existing tests were edited or deleted where they pinned removed
  mechanisms (enable lists, entry points, webhooks, in-process
  ingress, handler-based egress); their preserved intents moved to
  `test_zeta_connector_modules.py`, `test_zeta_fs_ipc.py`, and the
  wire fault drills. The suite is fully green (1102 passed).

## Known gaps

- Slack Socket Mode and Telegram long-polling are implemented from
  API knowledge and unit-tested at the parsing/protocol layer;
  neither has run against live credentials yet. First live runs
  should watch the serve log for their children.
- No runtime code mints blob-domain addresses yet (payloads stay
  inline until the Phase 2 blob store); the domain is specified and
  vectored.
- Ingress waves are computed at `zeta serve` startup; binding
  changes on disk need a restart.
- A connector serving both ingress and egress runs two children
  (one per role); Slack therefore opens its socket only in the
  ingress child, and Telegram polls only there.
- The `zeta.wire` SDK (`plugin.py` + `envelopes` + `framing`) is
  still inside zeta-os; extraction into a standalone SDK package is
  future work (its imports are already self-contained).
- Local-only: `uv sync` fails on this machine because the editable
  zeta-os build compiles the Rust TUI and local rustc (1.85) is
  older than its dependencies need (≥ 1.88); everything here ran
  with `uv run --no-sync` against the existing venv. CI runners are
  unaffected.

## Verifying the exit criteria

All commands from the repo root (drop `--no-sync` once local rustc
is updated):

```sh
# Spec, vectors, and both protocol sides
ls spec/wire-v0.md spec/vectors/README.md
uv run --no-sync pytest zeta/tests/test_zeta_wire_vectors.py zeta/tests/test_zeta_wire_plugin.py \
  zeta/tests/test_zeta_wire_host.py zeta/tests/test_zeta_wire_calls.py -q

# Connector executables: describe manifests, module logic, discovery
zeta-connector-filesystem --describe | python3 -m json.tool | head
uv run --no-sync pytest zeta/tests/test_zeta_connector_modules.py -q

# Runtime integration: discovery, waves, journal, dedupe, allowlist
uv run --no-sync pytest zeta/tests/test_zeta_fs_ipc.py zeta/tests/test_zeta_agents.py -q

# Addresses: vector match, determinism, legacy dual-read
uv run --no-sync pytest zeta/tests/test_zeta_addresses.py -q

# Entire suite (1102 passed, 2 skipped at time of writing; coverage 87%)
uv run --no-sync pytest -q

# Demo smoke (needs Codex credentials; verified end-to-end on
# 2026-08-11 after the cutover: one zeta-connector-filesystem child
# observed, summary produced, clean shutdown):
uv run --no-sync zeta new /tmp/zeta-demo
cd /tmp/zeta-demo && uv run --no-sync zeta serve &
echo 'Buy milk.' > /tmp/zeta-demo/inbox/todo.txt
sleep 60 && cat /tmp/zeta-demo/summaries/todo.txt.md
kill %1
```

The demo's LLM step cannot run in CI (no model credentials); the
ingress-to-journal half is covered by
`test_zeta_fs_ipc.py::test_ipc_ingress_reaches_the_journal` and the
agent-execution half by the worker tests with model stubs.
