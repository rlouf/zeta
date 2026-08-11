# Notarized deliverable: provenance a client can check without trusting you

A consulting shop ships a client report together with its provenance: the
report bytes, the source data it was derived from, a manifest of every
content-addressed object and derivation link between them, and the hash-chain
head of the run that produced it. The client verifies the whole bundle
offline — a ~300-line standalone Python script that recomputes every BLAKE3
address from bytes it can see. No network, no access to the consultant's
systems, no zeta installed. The claim "this report was derived from this data
by this recorded model call, in this run" becomes checkable arithmetic
instead of a statement to be trusted.

## Why other frameworks can't do this

- **LangGraph / CrewAI** trace runs into LangSmith or callbacks. You could
  export those traces, but they are mutable log rows keyed by run id: nothing
  in them is content-addressed, so a recipient has no way to recompute
  anything. Verification would mean trusting the exporter, which is the thing
  being avoided.
- **Temporal** durably records workflow history and could hand a client the
  history file. But payloads are opaque blobs with no hash addressing and no
  derivation edges; you would have to design the addressing scheme, the
  manifest, and the verifier from scratch — at which point you are building
  this substrate, not using Temporal's.
- **n8n** keeps per-node execution data for debugging with no integrity
  guarantees at all.

In Zeta the exported bundle is a projection of what the runtime already
stores: every object's id *is* the BLAKE3 hash of its canonical bytes, every
derivation names its exact inputs, and the journal chains every event to its
predecessor (spec/substrate-v0.md, spec/journal-v0.md). Because the address
schemes are frozen, backend-neutral specs, an independent verifier needs only
the hash function — the demo's `verify.py` imports the Python standard
library plus the `blake3` package and nothing else, and reproduces zeta's
domained addresses exactly (same derive-key contexts, same canonical JSON).

## How to run

Prerequisites:

- Python 3.11+ and `uv`
- Codex CLI logged in (`codex login`) — the run makes live model calls (one
  agent turn with one recorded model transformation; about 20-60 seconds)

```sh
./run.sh
```

Everything the script creates lives under `demo/notarized-deliverable/work/`,
which is recreated on every run.

## What you'll see and how to interpret it

1. **Scaffolding** — `zeta new` creates a project in `work/consultancy`; the
   `analyst` agent is generated from `analyst.md.tmpl` and the seed data is
   copied to `source/client-metrics.txt`.
2. **Publishing the trigger event** — `zeta events publish report.requested`
   enqueues the work durably.
3. **Running the agent** — the agent stores the client data verbatim as a
   content object (`ContentLiteral:v1`), derives the report through one
   `transform_content` model call (which records the exact prompt, response,
   and a `ModelTransform:v1` derivation naming its inputs), and calls
   `finish` with the report's content address. That finish call lands in the
   journal as a chained event.
4. **Exporting the bundle** — `export_bundle.py` (consultant side, uses
   zeta's store APIs) writes `work/bundle/`: `report.md` and
   `source/client-metrics.txt` (the exact bytes of the stored objects),
   `manifest.json` (the report's object closure, the derivation records, the
   run's 22 journal events, and the recomputed chain head as an anchor), and
   `verify.py`. Nothing is invented at export time: objects and derivations
   are read from the store; the chain entries are minted by zeta's own
   `journal_entry` and cross-checked with `verify_entries` before export.
5. **Offline verification** — the verifier runs from a different directory
   under `uv run --no-project --with blake3`, so zeta is not even importable.
   It prints PASS/FAIL for: file bytes against their plain content addresses
   and against the stored objects they claim to carry; every object address
   recomputed from its own canonical bytes; link closure; every derivation
   address and its endpoints; the report's lineage walk (report ←
   ModelTransform ← source + model response ← prompt); and the journal chain
   rebuilt event by event to the head, compared against both the manifest's
   anchor and one passed on the command line.
6. **Tamper check 1** — one bit flipped in a copy's `report.md`. The verifier
   names exactly which address no longer matches, printing both the recorded
   and the recomputed hash.
7. **Tamper check 2** — the chain-committed `finish` event in a copy's
   manifest is edited to claim a different deliverable. The recomputed chain
   head no longer matches the anchor, and the finish-binding check names the
   two conflicting object ids.
8. **Interpretation** — what the anchor adds: the manifest alone proves
   internal consistency; a head address received through a second channel
   (email, invoice, published hash) additionally proves the bundle is the
   same run the consultant committed to, because a substituted look-alike
   bundle recomputes to a different head.

Honesty notes:

- The verifier reproduces zeta's full domained addressing — object,
  derivation, and chain addresses, not just plain content hashes — because
  the derive-key contexts and canonical JSON rules are small and frozen in
  `spec/substrate-v0.md` and `spec/journal-v0.md`. Its one non-stdlib
  dependency is the `blake3` package (the same one zeta's Python uses); pure
  hashlib is not enough because BLAKE3 is not in hashlib.
- Canonical JSON identity of float payload values relies on Python's
  shortest-repr round-trip on both sides. All payloads in this demo are
  strings and integers, so the caveat is theoretical here.
- The chain entry addresses are minted at export time from the durable
  events, not read from extra columns — journal-v0 defines them as a pure
  function of the event sequence, and the export cross-checks its chain with
  zeta's own `verify_entries` before writing the manifest.
- The bundle's source file is written from the *stored* source object — what
  the run actually used — not from the seed file on disk. The agent is
  instructed to store the seed verbatim and did so byte-for-byte in testing;
  run.sh prints whether the two match, and the provenance claim is about the
  stored object either way.
- The bundle proves derivation structure and integrity, not model behavior:
  it shows exactly which prompt produced the report, not that the report is
  a *good* summary. And like any hash system, it authenticates data, not
  intent — a consultant could notarize a bad report perfectly.

## Where else this principle applies

- **Regulated deliverables** (audit reports, legal memos, clinical summaries):
  ship the evidence pack with the document; the regulator verifies lineage
  without access to your systems.
- **AI-disclosure compliance**: "which parts are model-generated, from what
  input" shipped as a checkable artifact rather than an attestation letter.
- **Software supply chain**: the same shape as SLSA provenance and in-toto
  attestations — content-addressed artifacts plus signed derivation
  statements — but generated as a byproduct of the runtime instead of bolted
  onto a build system.
- **Data escrow / vendor handoffs**: deliver datasets with their derivation
  closure so the receiving side can verify transformations before ingesting.
- **Air-gapped review**: security-sensitive clients (defense, healthcare) can
  verify on a machine with no network at all — the demo's verifier makes
  zero connections by construction.
