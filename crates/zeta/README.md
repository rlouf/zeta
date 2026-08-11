# zeta

`zeta` defines content identities and event-journal rules.
It does not select or connect to a database.

## Contents

The `substrate` module provides these items:

- Full `b3:` addresses.
- BLAKE3 hashing for bytes and files.
- Canonical JSON for identity data.
- `Object`, `Derivation`, `Ref`, and `RefUpdate` values.
- A file blob store with two-character directory fanout.

The `journal` module provides these items:

- Draft and durable event values.
- Canonical payload and chain addresses.
- Linked journal entries.
- Journal verification.
- `MemoryJournal` for in-memory use and conformance tests.

## Example

Create a content address for an object:

```rust
use zeta::substrate::Object;

let object = Object {
    kind: "example.message".to_owned(),
    schema: "example.v1".to_owned(),
    data: Default::default(),
    links: Vec::new(),
};

let address = object.content_address().expect("valid object");
assert!(address.to_string().starts_with("b3:"));
```

## Test

```sh
cargo test -p zeta
```
