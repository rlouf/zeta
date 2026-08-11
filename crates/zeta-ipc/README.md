# zeta-ipc

`zeta-ipc` gives connected processes one language for requests, events, and
lifecycle control. Shared rules keep every connection predictable when calls
overlap, arrive out of order, or cross in both directions.

## Contents

- Typed JSON-RPC requests, notifications, results, and errors.
- Initialization with `source`, `client`, and `provider` roles.
- Request correlation and flow control in both directions.
- Bounded NDJSON readers and writers.
- Validation for event, session, provider, ping, and shutdown messages.

## Example

Parse a request:

```rust
use zeta_ipc::{Message, RequestId};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let line = r#"{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}"#;
    let message = Message::parse_str(line)?;

    let Message::Request(request) = message else {
        unreachable!();
    };
    assert_eq!(request.id, RequestId::from(1_u64));
    assert_eq!(request.method, "ping");
    Ok(())
}
```

## Test

```sh
cargo test -p zeta-ipc
```
