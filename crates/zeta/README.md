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
- Separate application and lifecycle-control Unix-socket JSON-RPC endpoints.
- Native `up`, `down`, and `status` process lifecycle commands.
- A verified bridge from authored manifests to portable agent invocations.
- A compiled recurring scheduler backed by durable Dispatch journal facts.
- Conversion from verified project manifests to deterministic Dispatch routes.
- Conversion from successful agent results to typed Dispatch completions.
- Lifecycle timeouts and automatic process recycling after failures.
- A system clock, shared cancellation token, and UUID ID source.
- Callback adapters for observations and durable event drafts.
- End-to-end composition tests for models, native tools, effects, and providers.

Provider processes start when first used. The host initializes each connection,
checks the declared methods, sends direct calls, and reaps the whole process
group on cancellation, timeout, exit, shutdown, or drop.

`LocalSocketServer` binds an explicitly supplied absolute path, restricts the
socket to its owner, and serves multiple initialized Zeta IPC clients. The
application endpoint delegates host requests through a caller-provided
function, broadcasts durable event notifications, and disables client shutdown
requests. The distinct control endpoint exposes only lifecycle health and
shutdown authority. Both endpoints report the same runtime instance ID, and
cleanup removes a filesystem entry only while it still identifies the socket
created by that server.

`routes_from_project` is owned by this host because it is the composition point
between `zeta-manifest` and `zeta-dispatch`; those domain crates remain
independent.

`Scheduler` compiles cron expressions and IANA timezones from a verified project
before a tick writes anything. It owns activation, same-day backfill,
`catchup: latest`, missed-tick evidence, DST behavior, and the scheduler status
read model. Durable state is reconstructed from `zeta.scheduler.tick.*` journal
facts. Selected `agent.<slug>.scheduled` occurrences enter Dispatch through
ordinary idempotent ingress and use the same routes as connector events.
Dispatch does not parse calendar policy; future agent publications remain its
separate deferred-publication concern.

The native crate currently exposes this host operation without a native worker
loop. `zeta up` therefore means that the native process and control plane are
ready; application requests return `runtime_not_available`. A later worker
slice must construct `Scheduler` for the active project generation and call
`tick` before deferred publications, wait timeouts, and queue claims, matching
the existing Python worker order.

```rust
use zeta::Scheduler;

fn fire_due(
    project: &zeta_manifest::ProjectManifest,
    dispatch: &mut zeta_dispatch::Dispatch,
    now_ms: i64,
    ids: &mut dyn zeta_agent::IdSource,
) -> Result<Vec<zeta_journal::Event>, zeta::SchedulerError> {
    Scheduler::from_project(project)?.tick(dispatch, now_ms, ids)
}
```

`attempt_completion` is the corresponding composition point between
`zeta-agent` and `zeta-dispatch`. It retains durable answer, event, and usage
metadata; converts ordered publish, wait, and cancellation proposals without
renumbering them; and rejects content promotion until Dispatch can commit that
operation atomically. Dispatch then owns validation, execution order, and the
legacy control-array shape recorded in the journal.

## Prepare an authored agent

The host verifies the complete project and execution manifests once, then
retains the immutable model, capability, event, and executor projection:

```rust
use zeta::{prepare_agent, InvocationInputs};

fn prepare(
    project: &zeta_manifest::ProjectManifest,
    execution: &zeta_manifest::ExecutionManifest,
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

## Native lifecycle

Run the control plane in the foreground or detach it after a readiness
handshake:

```console
$ cargo run -p zeta -- up
running 12345

$ cargo run -p zeta -- up --detach
started 12346
```

Inspect or stop the runtime from the same project tree:

```console
$ cargo run -p zeta -- status
running

$ cargo run -p zeta -- status --json
{"status":"running","pid":12346,"instance_id":"...","project_root":"/...","state_dir":"/.../.zeta","socket":"/.../.zeta/runtime.sock","control_socket":"/.../.zeta/runtime-control.sock","detail":null}

$ cargo run -p zeta -- down
stopped
```

`--state-dir DIR` overrides project-local state discovery for all three
commands. `up --project-root DIR` changes the authored project root. Runtime
state uses an advisory lease, atomic owner metadata, separate owner-only
application and control sockets, and `runtime.log` for detached diagnostics.
`status` is read-only, and `down` never signals a PID read from metadata.
