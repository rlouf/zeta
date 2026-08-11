# zeta-ipc

`zeta-ipc` implements the wire-v0 plugin protocol.
It gives connected processes one message format and one set of session rules.

## Contents

- Typed envelopes for each protocol message.
- Parsing and validation for envelopes.
- Canonical JSON that preserves unknown fields.
- Blocking NDJSON readers and writers.
- Session state machines for each end of a connection.
- Stable rule tokens for protocol violations.
- Limits for inline payloads and frames.

The caller supplies the byte transport and all clock values.

## Example

Parse a heartbeat envelope:

```rust
use zeta_ipc::{Envelope, Kind};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let line =
        r#"{"id":"m-1","kind":"heartbeat","ts":"2026-08-10T12:00:00Z","v":0}"#;
    let envelope = Envelope::parse_str(line)?;

    assert_eq!(envelope.kind(), Kind::Heartbeat);
    assert_eq!(envelope.to_canonical_json(), line);
    Ok(())
}
```

## Test

```sh
cargo test -p zeta-ipc
```
