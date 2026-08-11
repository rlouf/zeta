# Phase 1.5 report — the substrate reshape

Phase 1.5 replaces `zeta-cas` with `zeta-substrate` and makes the Python and
Rust substrate implementations use one BLAKE3-only identity model. `Object`,
`Derivation`, `Ref`, and `RefUpdate` now have direct Rust mirrors. Both
languages consume the same canonical-encoding and active-address vectors.
`zeta-ipc` carries addresses as data and has no hashing dependency.

Remi changed the compatibility ruling during implementation: this phase is a
clean break. The previous epoch, compatibility parsers, fallback verification,
dual-read behavior, and obsolete identity field names were removed. Existing
local state from the removed epoch is not supported and must be regenerated.

## Commits

The implementation was split by concern:

1. freeze the Python encoding ground truth before changing the mint;
2. rename `zeta-cas` to `zeta-substrate`;
3. remove the substrate dependency from `zeta-ipc`;
4. move the Rust identity module to the substrate object vocabulary;
5. add the Rust Object, Derivation, Ref, and RefUpdate model;
6. change Python substrate identity and persisted field vocabulary;
7. replace the address vectors with the active BLAKE3 DAG;
8. regenerate the Phase 2 replay fixture without removed identifiers;
9. make IPC canonical JSON byte-identical to substrate canonical JSON.

The crate and module moves used Git moves before their implementation edits so
their histories remain traceable.

## Recon findings

The repository map in the brief was stale in two places. The Rust protocol
crate was already named `zeta-ipc`, not `zeta-wire`, and the TUI already lived
at `crates/zeta-tui`. Neither correction changed the scope of this phase.

The existing Python substrate was the design authority:

- `substrate/objects.py` defines immutable Objects, provenance Derivations,
  named Refs, and conditional RefUpdates.
- `substrate/store.py`, `memory.py`, and `sqlite.py` define the current Python
  store boundary. The Rust store port is still deferred.
- Object and Derivation construction is live in authoring, context building,
  context transforms, trace provenance and replay, content tools, and journal
  graph import.
- `substrate/sqlite.py::import_object` and derivation import recompute an
  address to verify imported content. `harness/project.py` also reconstructs
  skill Objects to verify project snapshots. These paths now verify only the
  current BLAKE3 address.
- Journal graph import delegates verification to the substrate store. It does
  not implement another mint.

Phase 0 had live event-, chain-, prompt-, and skill-domain outputs. Event
addresses identify source retransmissions. Chain addresses identify publish
and wait handles. Prompt addresses appeared in trace provenance, and skill
addresses appeared in project snapshots. The blob context had no call sites.

The clean-break ruling removed the prompt and skill mints instead of defining a
micro-epoch. Prompts and skills are now normal substrate Objects, so `kind`
separates them inside the object-domain identity. Project snapshot,
execution-manifest, prompt, and prompt-component schema versions were advanced
where their persisted fields changed. The generated replay fixture was rebuilt
without the two removed-epoch records; the journal schema itself was not
changed.

## Canonical encoding

`spec/vectors/substrate/encoding.json` was generated from the unmodified Python
substrate before the mint changed. It records exact UTF-8 text for simple,
nested, Unicode, null-heavy, escaping, numeric, and empty values. The active
Object and Derivation vectors were generated only after the new mint landed.

The byte rules are now normative in `spec/substrate-v0.md`:

- keys sorted by Unicode code point;
- compact separators and no trailing newline;
- non-ASCII text emitted directly as UTF-8;
- string object keys and ordered arrays;
- integers in the union of `i64` and `u64`;
- finite binary64 floats in Python's shortest round-trip spelling.

The float audit found real Rust/Python formatting differences. Rust's default
serializer omitted the positive exponent sign in `1e+30` and used fixed
notation at Python's `1e-05` scientific boundary. It also exposed a test trap:
with `serde_json` arbitrary precision enabled, values parsed from the vector
file can preserve their source spelling and hide the programmatic-float
difference. Both Rust crates therefore test vectors and independently-created
float values. Their writers add the exponent sign, pad short exponents,
preserve `-0.0`, keep `.0` on integral floats, and match the boundary.

All observed finite binary64 cases were matched. NaN, infinities, values that
cannot be represented as binary64, non-string object keys, and integers
outside the signed-or-unsigned 64-bit range are banned. Python rejects them
before an identity mint. Rust substrate returns `CanonicalJsonError`.
Result-returning IPC event paths report `bad_payload_number` before
serialization.

## Address model

The active operations are:

| identity | operation or context |
|---|---|
| exact content bytes | plain, undomained BLAKE3 |
| Object | `zeta-os 2026-08 cas object` |
| Derivation | `zeta-os 2026-08 cas derivation` |
| source-event retransmission identity | `zeta-os 2026-08 cas event` |
| publish and wait chain links | `zeta-os 2026-08 cas chain` |

Every address is full width: `b3:` plus 64 lowercase hexadecimal characters.
There is no alternate parser or verification entry point. Runtime ids such as
`evt_`, `qi_`, `att_`, `run_`, `pub_`, and `wait_` remain because they encode
runtime roles, not a hash algorithm. Event-domain BLAKE3 values are the stable
source identities used for wire retransmission and acknowledgment; durable
journal events still receive random `evt_` ids.

Python `addresses.py` owns plain BLAKE3, the four active derive-key contexts,
strict BLAKE3 syntax, and the shared canonical-byte guard used by identity
mints. `Object.content_address()` and `Derivation.content_address()` are the
substrate mints of record.

Rust `zeta-substrate` exposes `Hash`, `hash_bytes`, `hash_file`, `derive`, the
four value types, canonical JSON, and the `Fanout2` `BlobStore`. `Flat` was
removed. The crate has no journal logic, ref persistence, prefix resolution,
or Object store implementation.

## IPC dependency cut

`zeta-ipc` no longer imports `zeta-substrate` and no longer constructs event
addresses. `PluginSession::send_event` accepts a non-empty caller-supplied id.
The protocol keeps that id unchanged. Payload-address validation is a local
syntax check for `b3:` and 64 lowercase hexadecimal characters.

IPC has its own small canonical writer because a protocol layer must not link
a hashing implementation. It consumes the substrate encoding vectors but has
only `serde` and `serde_json` as runtime dependencies.

## Persisted vocabulary changes

The clean break renamed identity-bearing fields rather than preserving aliases:

- `source_address` replaces the source-file digest field;
- `payload_address` replaces the payload digest field;
- `content_address` replaces project source digest fields;
- `raw_content_address` and `text_address` replace prompt content digest fields;
- `SkillResource.object_id` replaces its digest field;
- `AgentSpec.content_address` replaces its source digest field.

Project snapshots are version 5 and execution manifests are version 4.
Prompt Objects and prompt-component Objects use their version 2 schemas. There
is no migration reader for earlier versions.

## Discretionary decisions

- The event context remains active only for source-event retransmission
  identity. It is not the durable journal event-id format.
- The canonical encoding is shared by substrate and wire instead of retaining
  wire's earlier rule that collapsed integral floats into integers.
- Rust `Object.data` and `Derivation.params` use JSON object maps. This mirrors
  Python's declared field types and makes non-string keys unrepresentable.
- The Phase 2 fixture generator was left structurally unchanged except for
  removing obsolete records, an unused import, and an unused binding.
  Regeneration changed fixture ids and timestamps because the existing
  generator intentionally uses live time and random ids.
- The untracked `spec/journal-v0.md` belongs to concurrent user work. It was
  neither edited nor staged.

## Deviations from the original brief

The original brief required old-address vectors, mixed-epoch DAGs, syntactic
epoch selection, and verification-only old mints. Remi explicitly replaced
that requirement with a clean break. Therefore:

- only active encoding and BLAKE3 address vectors remain;
- stores and import paths accept only current addresses;
- the Rust API contains no old mint or compatibility type;
- prompt, skill, and blob contexts are absent rather than reserved;
- no mixed-epoch test or data migration exists.

The Rust protocol crate is `zeta-ipc`, matching the repository, rather than
the name in the brief. The journal store port, journal schema work, connectors,
runtime behavior, TUI, stagefs, and publishing remained out of scope.

`rust-analyzer` SSR and `sg` were unavailable in the environment. The moves
used `git mv`; focused searches and `apply_patch` handled the implementation.
A requested Claude CLI planning review did not return, and Gemini CLI was not
installed. Repository tests and vectors remained the authority.

## Verification results

The final local matrix passed:

- Python: 1,164 passed and 2 skipped; full statement coverage is 90%.
- Focused Python address, import-boundary, trace, and wire tests: 226 passed.
- Rust IPC: 22 tests and 5 doctests passed.
- Rust substrate: 20 tests and 20 doctests passed.
- Full Rust workspace: the substrate, IPC, and 62 TUI tests passed with
  warnings denied on `nightly-2026-03-25`.
- Rustfmt and Clippy passed with warnings denied.
- Ruff, Ruff format, ty, vulture, complexipy, and full pre-commit passed.
- Radon reports the shared recursive identity validator at C(12), below the
  configured complexity limit of 25.
- The fixture database reports `ok` from `PRAGMA integrity_check` and contains
  14 active events.
- The IPC runtime dependency tree contains only `serde` and `serde_json`.
- The active tree contains no removed algorithm vocabulary. No `target/`,
  removed vector, or `zeta-cas` path is tracked.

## Reviewer commands

Run these commands from the repository root:

```sh
# Python canonical encoding, addresses, store import, event identity, and DAG
uv run pytest zeta/tests/test_zeta_addresses.py \
  zeta/tests/test_zeta_trace_cli.py \
  zeta/tests/test_zeta_wire_vectors.py -q

# Full Python suite and coverage gate
uv run pytest -q
uv run coverage run -m pytest
uv run coverage report

# Rust conformance and warning gates
cargo fmt --all -- --check
RUSTFLAGS="-D warnings" cargo test -p zeta-substrate -p zeta-ipc --locked
RUSTFLAGS="-D warnings" cargo clippy -p zeta-substrate -p zeta-ipc \
  --all-targets --locked -- -D warnings

# Protocol dependency boundary
cargo tree -p zeta-ipc --edges normal

# Move history, fixture, and clean-break audits
git log --follow --oneline -- crates/zeta-substrate
sqlite3 'file:zeta/tests/fixtures/phase2-journal/journal.sqlite3?immutable=1' \
  'PRAGMA integrity_check; SELECT count(*) FROM events;'
! git ls-files | rg '(^|/)target/|zeta-cas|zeta_cas|legacy-address'
! rg -a -n -i --hidden --glob '!.git/**' --glob '!notes.md' \
  --glob '!PHASE0-REPORT.md' --glob '!PHASE1-REPORT.md' \
  --glob '!PHASE1.5-REPORT.md' --glob '!spec/journal-v0.md' \
  --glob '!uv.lock' 'sha256|sha-256' .

# Workspace and repository gates (the TUI needs the repository's newer toolchain)
RUSTFLAGS="-D warnings" cargo +nightly-2026-03-25 test --workspace --locked
RUSTUP_TOOLCHAIN=nightly-2026-03-25 uv run pre-commit run --all-files
```

`cargo tree -p zeta-ipc --edges normal` must contain no BLAKE3 or substrate
crate. The active tree must contain no removed algorithm vocabulary outside
the two historical phase reports and lockfile integrity data.
