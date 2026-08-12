# zeta-agent

`zeta-agent` runs one resolved agent invocation. It gives scripted and live
runs the same prompt rules, tool behavior, event proposals, and
content-addressed trace.

## Contents

- Ordered prompt construction, history repair, and compaction.
- Chat Completions and Responses model streams.
- Capability grants and JSON Schema validation.
- Native file, search, shell, URL, and web-search tools.
- Content queries, transforms, and finish operations.
- Publish, wait, and cancel proposals.
- Durable lifecycle records for side effects.
- Bounded streaming and cooperative cancellation.
- Explicit environment, time, identity, network, and persistence inputs.

## Prompt example

Build a prompt from explicit inputs:

```rust
use zeta_agent::{build_prompt, PromptEnvironment, PromptInput};

let input = PromptInput {
    objective: "Summarize the release.".to_owned(),
    system: Some("Answer plainly.".to_owned()),
    selected_model: Some("unit-model".to_owned()),
    ..PromptInput::default()
};
let environment = PromptEnvironment {
    working_directory: "/workspace/zeta".to_owned(),
    calendar_date: "2026-08-12".to_owned(),
};

let prompt = build_prompt(&input, &environment)?;
assert_eq!(prompt.model_input.messages.len(), 2);
assert!(prompt.prompt_object_id.starts_with("b3:"));
# Ok::<(), zeta_agent::AgentError>(())
```

`AgentRunner` uses the same prompt path inside a complete scripted or live
invocation.

## Capability aliases

Hosts that project authored manifests resolve each capability with its authored
model-facing name. This keeps canonical routing identity separate from the name
shown to the model:

```rust
use zeta_agent::{native_capabilities, ToolProfile};

let capabilities = native_capabilities();
let authored = ToolProfile::Native.resolve_capability(&capabilities[0], "search_code");
assert_eq!(authored.model_name, "search_code");

let codex_builtin =
    ToolProfile::Codex.resolve_capability(&capabilities[1], "run_shell");
assert_eq!(codex_builtin.model_name, "exec_command");
```

Ordinary aliases also survive Codex resolution. Codex built-ins override an
authored alias when their adapter requires a canonical model name or argument
rewrite; for example, `zeta.bash` is exposed as `exec_command` and maps `cmd`
back to canonical `command`.

## Test

```sh
cargo test -p zeta-agent
```
