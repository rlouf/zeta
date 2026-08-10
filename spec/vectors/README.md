# wire-v0 conformance vectors

Golden test data for `spec/wire-v0.md`. A third-party SDK (Go,
TypeScript, Rust, …) consumes these files directly to prove it speaks
the same protocol byte-for-byte. Zeta's own Python test suite reads
these exact files — there are no copies.

## `envelopes/valid/*.json`

One envelope per file, stated in **canonical form** (spec §2.1: sorted
keys, compact separators, literal UTF-8) plus a trailing newline. A
conforming implementation must:

1. parse each file into its envelope model without error;
2. re-serialize the parsed JSON canonically and reproduce the file's
   bytes exactly, minus the trailing newline.

`heartbeat-unknown-field.json` carries a field no v0 kind defines; it
is valid because unknown envelope fields must be ignored (spec §3).

## `envelopes/invalid/*.json` and `*.reason.txt`

One envelope per `.json` file that a conforming implementation must
**reject**. The sibling `<name>.reason.txt` documents why:

- line 1 is a stable machine-checkable rule token (e.g.
  `payload_choice`, `bad_timestamp`, `missing_field:kind`). A
  conformance test should assert its validator reports this rule (or
  its own documented mapping of it);
- the remaining lines are prose citing the violated spec section.

## `handshake/session-01.jsonl` and `session-02.jsonl`

Complete scripted sessions. `session-01` exercises the source path:
hello → hello_ack → two events with acks → heartbeat → shutdown.
`session-02` exercises operations: hello declaring an operation →
hello_ack with `config` → call → call_result → heartbeat →
shutdown. Each line is a canonical envelope with **one extra
field**, `_dir`, the direction marker:

- `"c2p"` — child (plugin) to parent (runtime);
- `"p2c"` — parent to child.

Strip `_dir` before validation; the remainder must validate as the
appropriate envelope. A plugin-side implementation must be able to
replay the session as the child (emitting the `c2p` lines, given the
`p2c` lines as input); a runtime-side implementation must replay it as
the parent. Event ids in the session are true `b3:` event-domain
addresses of the identity described in spec §6.1.

## `addresses/vectors.json`

Inputs with their expected `b3:` addresses for every domain context in
spec §11. Each vector gives the input bytes as UTF-8 text
(`input_utf8`) or base64 (`input_base64`) and the expected address. An
implementation's `derive_key` output must match byte-for-byte —
including for the empty input.
