# Executor examples

These projects select a named executor profile. The profile selects a trusted
platform driver. The agent does not name the platform driver directly.

Install the matching Python executor driver before you run either project.
Install `zeta-executor-modal` for Modal. Install `zeta-executor-daytona` for
Daytona. The `reuse` field selects the environment lifetime. `call` creates one
environment for each capability call. `session` and `durable` send a stable
instance name to the driver. The driver must attach to that name or create it.

The Modal profile must set `modal.app`. An optional `modal.image` selects a
published image. The Daytona profile can set `daytona.snapshot`. The snapshot
must contain Python 3.11 or newer. Both drivers apply `network: none` at
sandbox creation.

Zeta sends a verified workspace bundle and a tool bundle with each open call.
The driver must verify each file content address before it transfers the files.
The tool bundle has each capability identifier, description, input schema,
output schema, and source path. The open allow-list contains every capability
in that bundle. Later calls send only a handle, capability identifier, and input.

Each `capabilities/*.yaml` file declares one portable tool schema. Zeta rejects
a descriptor that differs from its Python tool declaration.

The two projects use the same tool interface. This proves that an agent can
change execution platform without tool changes.
