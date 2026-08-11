# zeta-agent

`zeta-agent` runs one resolved agent invocation. It keeps prompt identity,
tool-call behavior, event proposals, and traces deterministic while the caller
supplies the model, tools, clock, cancellation, and IDs.

## Contents

- Ordered prompt construction from an objective, history, context, and tools.
- A provider-neutral model and tool loop.
- Capability grants and JSON Schema validation.
- Publish, wait, cancel, and return proposals.
- Immediate lifecycle records for side effects.
- Content-addressed prompt and execution traces.
- Explicit environment, time, cancellation, and identity inputs.

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

## Test

```sh
cargo test -p zeta-agent
```
