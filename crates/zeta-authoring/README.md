# zeta-authoring

`zeta-authoring` turns agent Markdown into validated declaration values.
A single parser keeps authored projects deterministic for every runtime host.

## Contents

- Agent, model, executor, retry, and schedule declarations.
- Ingress and egress connector bindings.
- YAML frontmatter validation with JSON-shaped extension values.
- Exact Markdown body preservation.
- Plain `b3:` identity for the original source bytes.
- Structured errors for source and field diagnostics.

## Authoring profile

Frontmatter uses a portable YAML profile. Values must also be valid JSON:

- Object keys are strings.
- Numbers are finite, and integers fit signed or unsigned 64-bit values.
- `true` and `false` are booleans. Words such as `yes`, `no`, `on`, and `off`
  are strings.
- Dates, leading-zero numbers, sexagesimal values, and numbers with separators
  are strings.
- `0b`, `0o`, and `0x` forms are integers. Finite exponent notation is a
  number; an overflowing exponent remains a string.
- Duplicate keys, merge keys, custom tags, and cyclic values are rejected.
- Base directories are preserved as absolute paths, `~`, or `~/...`. Resolving
  a home directory is left to the caller that executes the declaration.

## Example

Parse an agent from bytes supplied by a host:

```rust
use std::path::Path;

let source = b"---\nname: Worker\ndescription: Does work.\n---\nDo the work.\n";
let spec = zeta_authoring::parse_agent(Path::new("worker.md"), source)?;

assert_eq!(spec.slug, "worker");
assert_eq!(spec.instructions, "Do the work.\n");
# Ok::<(), zeta_authoring::SpecError>(())
```

## Test

```sh
cargo test -p zeta-authoring
```
