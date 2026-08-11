# zeta

`zeta` is the native application package and executable for the Zeta product.
It sits at the top of the Rust dependency graph and will compose the runtime
domains into installed commands.

## Current boundary

The package is binary-only. It currently reserves the application and
executable name while the agent and Dispatch application layers are ported.
It intentionally exposes no library facade and has no command behavior yet.

Foundation code now has explicit owners:

- `zeta-substrate` owns content identity and blob primitives.
- `zeta-journal` owns event values, chaining, and verification.

Domain crates depend on those packages directly. They do not depend on this
application package.

## Build

```console
$ cargo build -p zeta
```
