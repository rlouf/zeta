# Verified audit trail: the journal is evidence, not logs

An approvals clerk agent processes three expense requests; every input event,
model call, tool call, and typed decision lands in the same append-only
journal. Afterwards an auditor does not read logs — they mint the journal-v0
hash chain over the whole journal with zeta's own library, anchor the single
head address in an external notary file, and then *verify*. Deleting the last
event in a copy of the database passes unanchored verification but fails the
anchor check (`expected_head`). Flipping one byte of a stored payload in a
copy fails at the exact entry (`payload_encoding`, event id, cursor, entries
already checked), printing the committed canonical bytes next to the tampered
ones.

## What machinery this uses, honestly

Chain identity is Phase 2 of the Rust port: a backend-neutral definition of
payload addresses, chain entry addresses, and verification rules, normative in
`spec/journal-v0.md` and implemented in `zeta/src/zeta/journal/types.py`
(`journal_entry`, `verify_entries`, stable divergence reasons). The live
runtime journal that `zeta run` writes stores **logical events only** — its
SQLite `events` table has no chain-identity columns yet; recording chain
identity per event at append time is the Dispatch storage phase, currently in
flight. So this demo does what a conforming storage backend will do: `audit.py
export` reads the logical events out of the runtime journal and mints each
entry's identity with zeta's own library, and `audit.py verify` re-checks the
database against that committed identity, entry by entry, per journal-v0 §7.

The trust model is exactly the spec's: the chain export and the database are
both untrusted evidence. Unanchored verification proves only internal
consistency of the retained sequence — a forger who recomputes the whole
chain stays internally consistent. "Tamper-evident" holds only relative to
the externally anchored head (here, a file standing in for a notary or WORM
log). That is journal-v0's own claim, and this demo does not claim more.

## Why other frameworks can't do this

- **Temporal** has a per-workflow event history and uses it for replay
  determinism, but it is not content-addressed and there is no defined
  procedure for a third party to verify it against a hash chain or an
  external anchor. Trust in the history is trust in the Temporal cluster and
  its database.
- **LangGraph** checkpoints are mutable state snapshots keyed by thread;
  nothing commits one checkpoint to its predecessor, so history can be
  edited or truncated with no verifiable trace.
- **CrewAI / n8n** persist execution logs. Logs answer "what does the
  database say happened"; they cannot answer "is what the database says the
  same thing that happened", because a log row carries no commitment to the
  rows before it.
- The primitive here — every event's identity commits every field, the
  canonical payload bytes, and the previous entry's address, with a
  spec-frozen byte format shared by the Python and Rust implementations — is
  what makes the journal *evidence* rather than logs. Any system could bolt a
  hash chain onto its logs; Zeta defines it normatively over the same journal
  the runtime itself executes from.

## How to run

Prerequisites:

- Python 3.11+ and `uv`
- Codex CLI logged in (`codex login`) — the demo makes three live model calls

```sh
./run.sh
```

Everything runs inside `work/` (recreated each run). One full run takes about
30-60 seconds, most of it the three model calls. The tampering and truncation
steps only ever touch copies of the database under `work/audit/`; the real
project journal is never modified.

## What you'll see and how to interpret it

- **Clerk runs**: three `expense.request` events are published and `zeta run`
  drains the queue. The clerk publishes a typed `expense.decision` for each
  (REQ-2 at $812 is rejected against the $500 policy). The decision events go
  back through the journal like everything else.
- **Audit step 1 (export)**: the chain is minted over *all* 37 journal events
  — a type histogram shows the requests, the `zeta.model_call.completed`
  events, the tool calls, and the decisions are all inside the chain. The
  model's own reasoning steps are committed history, not side logs.
- **Audit step 2 (unanchored verify)**: `VERIFIED (unanchored): 37 entries
  checked` plus the head address. This proves internal consistency only, and
  the script says so.
- **Audit step 3 (anchor)**: the head address is written to
  `work/notary/expense-journal.head` — 64 hex characters in an external
  system are the entire trust root. Anchored verification passes.
- **Attack 1 (truncation)**: the last event is deleted from a copy.
  Unanchored verification of the copy *passes* (36 entries — an honest
  demonstration of unanchored verification's limit), while anchored
  verification fails with reason `expected_head`. Matching the anchor is what
  detects deletion of the final suffix.
- **Attack 2 (byte flip)**: one byte of REQ-2's stored payload is changed
  (`812.0` → `312.0`) in a copy. Verification fails with the structured
  divergence journal-v0 §7 requires: reason `payload_encoding`, the exact
  event id, cursor 2, one entry checked before the divergence — and the
  script prints the committed canonical bytes beside the tampered ones, so
  the auditor sees precisely which fact was rewritten.
- **Closing check**: the real journal still verifies against the anchor,
  proving the attacks only ever ran against copies.

What an auditor gets here that logs cannot give: a claim of the form "these
37 records, in this order, with these exact bytes, are the complete history
up to the anchored head" — checkable by anyone with the library and the
64-character anchor, with disagreements localized to a named check at a named
entry.

## Where else this principle applies

- **Financial controls (SOX, internal audit)**: approval trails whose
  integrity is verified, not asserted — an auditor checks a hash chain
  against a quarterly-notarized head instead of trusting database access
  controls.
- **AI incident forensics**: after a harmful agent action, prove the recorded
  prompt/tool/decision sequence was not cleaned up after the fact; the model
  calls themselves are chain entries.
- **Regulated decision records** (lending, insurance, medical triage): each
  automated decision commits to the exact inputs it saw; rewriting an input
  to justify a decision later is detectable at the exact record.
- **Supply-chain and build provenance**: append-only build/attestation event
  logs where a periodically published head address makes silent history
  rewrites evident, in the same spirit as certificate transparency.
- **Cross-organization handoffs**: two parties who each keep a copy of the
  journal need only exchange head addresses to prove neither side's copy has
  diverged.
