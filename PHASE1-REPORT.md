# Phase 1 report — foundations (`zeta-cas` + `zeta-wire`)

wire-v0 now has two conforming implementations in two languages,
sharing one set of golden files. The Rust workspace at the repo root
holds `crates/zeta-cas` and `crates/zeta-wire`, both warning-free
under `RUSTFLAGS="-D warnings"`, both proven against
`spec/vectors/` read in place. The content-vs-derived hashing rule
is written into the spec, the vectors, and both implementations; the
`blob` derive-key context is retired before use. stagefs was not
touched (no checkout even exists on this machine), and the
spec-coupling ruling is recorded in `spec/wire-v0.md` §11. Commits:

1. `Rule: content addresses are plain blake3; retire the blob context`
2. `Author zeta-cas: hashes, domains, epochs, and a blob store`
3. `Implement wire-v0 in Rust: the sans-IO zeta-wire crate`
4. this report (with the fresh-clone check and a Rust CI job)

## Recon findings

- All Phase 0 prerequisites exist: `spec/wire-v0.md`,
  `spec/vectors/{envelopes,handshake,addresses}`,
  `zeta/src/zeta/wire/`, `zeta/src/zeta/addresses.py`.
- **The blob domain had zero call sites** — `blob_address` was
  defined but never called anywhere in `zeta/` — so the §5
  precondition held and the reconciliation proceeded.
- **The brief's kind list was stale.** Phase 0's late amendments
  added `call`/`call_result` as real kinds, `operations` on `hello`,
  and `config` on `hello_ack`; `session-02.jsonl` exercises them.
  Since conformance is vector-driven, `zeta-wire` implements the
  spec as landed, not the brief's seven-kind list.
- Phase 0's discretionary conventions, conformed to: invalid vectors
  annotate via sibling `.reason.txt` whose first line is a stable
  rule token; handshake sessions carry a `_dir` field (`c2p`/`p2c`)
  stripped before validation; canonical JSON is sorted-keys,
  compact, literal UTF-8.
- No stagefs checkout is available as a sibling (not a blocker per
  the brief). The mmap hashing path uses the `blake3` crate's own
  `Hasher::update_mmap` — memory-maps large files, plain reads for
  small ones — which is the behavior the reference implements,
  without reproducing any code.
- `rust/` keeps its own Cargo workspace (the TUI), so the new root
  workspace with `members = ["crates/*"]` coexists without
  exclusions; `cargo metadata` confirms exactly `zeta-cas` and
  `zeta-wire` as members.

## The §5 reconciliation

- `spec/wire-v0.md` §11 now defines the two address kinds
  normatively: content addresses are plain undomained BLAKE3
  (`payload_hash` in §6 amended accordingly); derived identifiers
  keep derive-key mode with the four frozen contexts. The rationale
  paragraph records that content hashing is domainless so one hash
  universe holds across sibling tools (stagefs named explicitly),
  that the coupling is by specification only, and that the missing
  dependency edge is deliberate.
- The `zeta-os 2026-08 cas blob` context is documented as **retired
  before use** and stays reserved; `addresses.py` keeps it as
  `RETIRED_BLOB_CONTEXT` with the same statement, and
  `blob_address` became `content_address` (plain BLAKE3).
- `spec/vectors/addresses/vectors.json` regenerated: four derived
  domains plus a `content` set of plain-BLAKE3 expectations (20
  vectors); the `event-payload-hash` valid envelope re-minted with
  the plain content address. Vectors were regenerated from Python
  and verified from Rust — each side checks the other.

## Discretionary decisions

- **`Id` grew two variants** beyond the brief's sketch:
  `LegacySha64` (Phase 0's skill hashes were bare 64-hex, and the
  Python `is_legacy` recognizes them) and `Opaque` (structural ids
  like `evt_…`/`qi_…` make `parse_id` total without pretending they
  belong to a hash epoch). `Id::is_legacy` matches the Python
  contract exactly, tests pin the parity.
- **`BlobStore` paths include the `blobs/` component** under the
  given root (`<root>/blobs/<hex>`, `<root>/blobs/ab/<rest>`),
  matching the brief's literal layout strings. Construction touches
  no filesystem; directories appear on first write. `put` uses
  tempfile-plus-persist in the destination directory; a losing race
  with an existing blob is success (equal address, equal bytes).
- **Sessions take time as two caller-supplied inputs** — a
  monotonic `Instant` for deadlines and a wall-clock RFC 3339
  string for stamping outgoing envelopes. A sans-IO machine that
  reads a clock cannot be replayed against the session vectors.
- **Envelope round-trips preserve unknown fields**: each struct
  carries a flattened `extra` map, so "ignore unknown fields" holds
  semantically while `heartbeat-unknown-field.json` still
  round-trips byte-for-byte. `heartbeat_secs` is a
  `serde_json::Number` so `10` never becomes `10.0`.
- **Rule tokens are strings, not an enum**: the `.reason.txt` files
  are the shared oracle across implementations, and the Rust
  validator is a check-for-check port of the Python one (same
  ordering) so both reject each vector with the same token.
- **Timestamp validation is hand-rolled** (fixed grammar plus real
  calendar checks, leap years included) to keep the pinned
  dependency tree minimal: blake3, serde, serde_json, tempfile, and
  dev-only base64/proptest, all pinned exactly.
- **Session replays compare structure, not message ids**: message
  ids are free strings per spec, so the replay tests compare
  kind-specific fields — and event ids exactly, because those are
  deterministic `b3:` event-domain mints that must match the vector
  bytes across languages (they do; `zeta-wire` mints them through
  `zeta-cas`, which is the planned dependency edge).
- The address-vector conformance test lives in `zeta-cas` (the
  owner of addressing) rather than `zeta-wire` as the brief's §6
  grouped it; both crates read the same file regardless.
- A minimal `rust` job was added to CI (`cargo build/test
  --workspace --locked` with `-D warnings`) so the workspace gates
  are enforced, not just documented.

## Deviations from the brief

- `Envelope` covers nine kinds, not seven (see recon: the spec as
  landed includes `call`/`call_result`), and `Session` grew the
  matching surfaces (`send_call`/`CallResolved` on the runtime side,
  `HandleCall`/`complete_call` on the plugin side) because
  `session-02.jsonl` is a golden file and must replay.
- No other deviations. stagefs untouched; `rust/zeta-tui` untouched;
  no runtime, journal, or daemon code; Python edits confined to the
  §5 reconciliation (`addresses.py`, its tests, the two vector
  files, the vectors README).

## Known gaps

- The tokio adapter for `zeta-wire` is deferred to Phase 3, as the
  brief directs; the blocking framer is the only IO surface.
- `PluginSession` always declares `operations` (possibly empty) in
  its hello, like the Phase 0 Python SDK; `session-01.jsonl`
  predates that field, so the plugin-side replay compares the
  vector's fields rather than full-envelope equality (exactly as
  the Python replay test does).
- Local note, unchanged from earlier phases: `uv sync` fails on
  this machine because the zeta-os editable build compiles the TUI
  and local rustc (1.85) is below its dependencies' MSRV; the new
  workspace crates build fine on 1.85 (`rust-version = "1.85"`),
  and Python ran via `uv run --no-sync`.

## Verifying the exit criteria

```sh
# 1. Workspace builds and tests, warning-free (10 test binaries green)
RUSTFLAGS="-D warnings" cargo build --workspace
cargo test --workspace

# 2. Cross-language conformance against the same files
cargo test -p zeta-cas --test vectors       # address vectors, Rust
cargo test -p zeta-wire --test vectors      # envelopes + sessions, Rust
uv run --no-sync pytest zeta/tests/test_zeta_addresses.py \
  zeta/tests/test_zeta_wire_vectors.py -q   # the same files, Python
uv run --no-sync pytest -q                  # full Python suite (1104 passed)

# 3. Fresh-machine check: a clean clone needs no sibling checkout
git clone . /tmp/zeta-fresh && cd /tmp/zeta-fresh
RUSTFLAGS="-D warnings" cargo build --workspace && cargo test --workspace

# 4. The reconciliation is in force
grep -n "Retired before use" spec/wire-v0.md
grep -rn "cas blob" zeta/src crates/   # only the reserved-string mentions
```
