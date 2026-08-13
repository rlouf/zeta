# zeta-manifest

`zeta-manifest` turns supplied agent bytes and host declarations into
validated, language-neutral manifests. Its pure APIs keep compiled projects
deterministic for every runtime host.

## Contents

- Agent, model, executor, retry, and schedule declarations.
- Ingress and egress connector bindings.
- Draft 2020-12 event schemas and returned-event schema derivation.
- Jinja-compatible prompt validation and rendering against `event`.
- Flat skill resources and pure `SKILL.md` declaration parsing.
- Language-neutral connector descriptions with stable implementation
  fingerprints.
- Whole-project reference validation and deterministic capability/skill
  inheritance.
- Strict version 1 project and per-agent execution manifests.
- YAML frontmatter validation with JSON-shaped extension values.
- Exact Markdown body preservation.
- Exact validated UTF-8 source retention for nested identity verification.
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

## Parsing and loading

Parse exact bytes without filesystem access:

```rust
let source = b"---\nname: Worker\ndescription: Does work.\n---\nDo the work.\n";
let spec = zeta_manifest::parse_agent("worker", source)?;

assert_eq!(spec.slug, "worker");
assert_eq!(spec.instructions, "Do the work.\n");
# Ok::<(), zeta_manifest::AgentSpecError>(())
```

Load an authored file and derive its slug from the filename:

```rust,no_run
use std::path::Path;

let spec = zeta_manifest::load_agent(Path::new("agents/worker.md"))?;

assert_eq!(spec.slug, "worker");
# Ok::<(), zeta_manifest::AgentSpecError>(())
```

Direct parse errors have no path. Loading attaches the supplied filesystem
path to parse and I/O errors.

## Schemas, prompts, and skills

Event declarations are merged into a sorted registry. Equal declarations are
idempotent, conflicting declarations are rejected, and every retained schema
is checked as JSON Schema Draft 2020-12. Returned-event schemas are derived
from that registry and keep branch-local definitions isolated.

Prompt templates may read the supplied `event` value and introduce local
template variables. Other undeclared roots are rejected before execution.
Rendering performs no autoescaping and follows Jinja's lenient missing-member
behavior:

```rust
let spec = zeta_manifest::parse_agent(
    "worker",
    b"---\nname: Worker\ndescription: Works.\n---\n{{ event.payload.text }}",
)?;
zeta_manifest::validate_prompt(&spec).unwrap();

let event = serde_json::json!({"payload": {"text": "<ready>"}});
assert_eq!(zeta_manifest::render_prompt(&spec, &event).unwrap(), "<ready>");
# Ok::<(), Box<dyn std::error::Error>>(())
```

Flat project skills and `SKILL.md` declarations are distinct. The host supplies
their exact bytes; this crate neither discovers search roots nor expands skill
directives. A flat resource receives a `zeta.skill.v1` substrate identity,
while `parse_skill` extracts portable metadata and preserves the body.

Connector declarations follow the same boundary: `parse_connector` validates
a supplied describe value and drops launch commands or process metadata. The
host supplies a stable implementation fingerprint and remains responsible for
discovery and execution.

## Project compilation and manifests

`compile_project` accepts declarations the host has already discovered. It
merges event schemas, adds scheduled events, resolves tool aliases to canonical
capability ids, applies skill and capability inheritance, grants agent-owned
capabilities only to their owner, and validates every prompt, event reference,
executor provider, extension, and connector binding. Disabled agents remain in
the normalized project.

```rust
let agent = zeta_manifest::parse_agent(
    "worker",
    b"---\nname: Worker\ndescription: Works.\n---\nWork.\n",
)?;
let implementation = zeta_manifest::ImplementationFingerprint::new(
    zeta_substrate::hash_bytes(b"native runtime"),
);
let project = zeta_manifest::compile_project(zeta_manifest::AgentProjectInput {
    agents: vec![agent],
    events: zeta_manifest::EventRegistry::new(),
    skill_resources: vec![],
    skill_specs: vec![],
    connectors: vec![],
    capabilities: vec![],
    executor_providers: vec![zeta_manifest::ExecutorProviderSpec {
        id: "local".to_owned(),
        implementation: implementation.clone(),
    }],
    model: None,
    runtime_fingerprint: implementation,
})?;

let project_manifest = zeta_manifest::project_manifest(&project)?;
let execution = zeta_manifest::execution_manifest(
    &project,
    &project_manifest.id,
    "worker",
)?;
assert_eq!(execution.project_revision, project_manifest.id);
# Ok::<(), Box<dyn std::error::Error>>(())
```

Manifest ids hash the substrate canonical JSON of the typed body. Restoration
rejects unknown fields, unsupported schema versions, non-normalized values,
tampered skill identities, mismatched project revisions, and altered
execution projections. Manifests contain declarations and stable
implementation fingerprints, never connector launch commands, reflected
Python callables, source paths, environment state, or secrets.

The native `zeta` host converts a verified execution manifest into
`zeta-agent`'s resolved invocation types. Neither crate depends on the other,
and authoring manifests are audit/compilation values rather than runtime
services.

## Test

```sh
cargo test -p zeta-manifest
```
