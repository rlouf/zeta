# zeta

`zeta` is the native application host. It gives runtime resources and provider
processes one clear lifetime inside an operating-system process.

## Why it exists

The host owns details that belong to a running application: child processes,
wall-clock time, cancellation, generated IDs, observations, and durable event
recording callbacks. Keeping these choices here makes the runtime logic easy to
embed and test.

## What it contains

- A supervised executor for installed capability providers that speak Zeta IPC.
- A verified bridge from authored manifests to portable agent invocations.
- Conversion from verified project manifests to deterministic Dispatch routes.
- Conversion from successful agent results to typed Dispatch completions.
- Lifecycle timeouts and automatic process recycling after failures.
- A system clock, shared cancellation token, and UUID ID source.
- Callback adapters for observations and durable event drafts.
- End-to-end composition tests for models, native tools, effects, and providers.

Provider processes start when first used. The host initializes each connection,
checks the declared methods, sends direct calls, and reaps the whole process
group on cancellation, timeout, exit, shutdown, or drop.

`routes_from_project` is owned by this host because it is the composition point
between `zeta-authoring` and `zeta-dispatch`; those domain crates remain
independent.

`attempt_completion` is the corresponding composition point between
`zeta-agent` and `zeta-dispatch`. It retains durable answer, event, and usage
metadata; converts ordered publish, wait, and cancellation requests without
renumbering them; and rejects content promotion until Dispatch can commit that
operation atomically. Dispatch then owns validation, execution order, and the
legacy control-array shape recorded in the journal.

## Prepare an authored agent

The host verifies the complete project and execution manifests once, then
retains the immutable model, capability, event, and executor projection:

```rust
use zeta::{prepare_agent, InvocationInputs};

fn prepare(
    project: &zeta_authoring::ProjectManifest,
    execution: &zeta_authoring::ExecutionManifest,
    inputs: InvocationInputs,
) -> Result<zeta_agent::AgentInvocation, zeta::PrepareAgentError> {
    let agent = prepare_agent(project, execution)?;
    agent.invocation(inputs)
}
```

`InvocationInputs` supplies trigger-specific identities, budgets, dates, and
directories explicitly. `PreparedAgent::capabilities` provides the same
alias-aware routes to `AgentRunner`, while executor selection remains available
to the host that owns provider lifecycle.

## Build

```console
$ cargo build -p zeta
```
