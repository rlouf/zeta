# Native CLI

The native CLI runs one explicit Zeta project.

Use `zeta --help` to see all commands.

## Quick start

A project has an `agents/` directory. It contains direct Markdown agent files.

Validate the project before activation.

```sh
zeta check --project-root ~/my-project
```

Activate the checked source.

```sh
zeta reload --project-root ~/my-project
```

Start the runtime in the background.

```sh
zeta up --detach --project-root ~/my-project
```

Inspect the active revision.

```sh
zeta status --project-root ~/my-project
```

Stop the runtime. The active revision remains available.

```sh
zeta down --project-root ~/my-project
```

## Commands

### `zeta check`

`check` reads the current `agents/` source. It validates every direct Markdown
file. It writes no runtime state.

Use it before `reload` in a script or review step.

```sh
zeta check --project-root ~/my-project
```

### `zeta reload`

`reload` validates source and atomically activates a new revision. A running
runtime uses the new revision after reload completes.

An edited agent keeps its previous active version until a successful reload.
A new agent remains inactive until a successful reload. A failed reload keeps
the previous revision.

```sh
zeta reload --project-root ~/my-project
```

### `zeta up`

`up` starts the runtime with the active revision. It does not read draft
source. Run `reload` first.

Use `--detach` to start the runtime in the background.

```sh
zeta up --detach --project-root ~/my-project
```

Without `--detach`, `up` stays in the foreground. Press `Ctrl-C` to stop it.

### `zeta status`

`status` reports runtime health. It also reports the deployed revision and
its enabled agents. It reports the deployed revision after `down` too.

```sh
zeta status --project-root ~/my-project
```

Use `--json` for scripts. The command writes one JSON object to standard
output. It writes no other data there.

```sh
zeta status --json --project-root ~/my-project \
  | jq '.active_project.agents[].slug'
```

The JSON document has `schema` equal to `zeta.status` and `version` equal to
`2`. The `status` field is one of `running`, `stopping`, `stopped`, `stale`, or
`degraded`.

`active_project` is absent if no revision exists. If present, it contains
`revision_id` and an `agents` list. Each list item has `slug`, `name`,
`description`, `schedule_count`, and `source_address`.

The CLI can add fields in later versions. Scripts must ignore fields they do
not use. A breaking change requires a new version.

### `zeta down`

`down` stops the runtime. It preserves the active revision and state.

```sh
zeta down --project-root ~/my-project
```

The command succeeds if the runtime is already stopped.

## State change

This release changes project revision fields and the native dispatch schema.
It does not open earlier preview state.

Use a new state directory, or back up and remove the old state directory.
Then run `zeta reload` before `zeta up`.

## Runtime state

By default, `up` and `reload` search for `.zeta` from `--project-root`.
`status` and `down` search from the current directory. Pass `--project-root`
to make all commands use the same project. Pass `--state-dir` to select an
exact state directory. `ZETA_STATE_DIR` also selects a state directory.

The selection order is `--state-dir`, `ZETA_STATE_DIR`, then `.zeta` discovery.

## Exit status

`check`, `reload`, `up`, and `down` return zero after success. `status` returns
zero only when the runtime is `running`. A command returns one after an
operational error. Invalid command arguments return two.
