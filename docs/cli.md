# Sigil CLI contract

Sigil's human-readable output is allowed to evolve. Machine-readable output is
available through explicit `--json` flags and should remain stable across
compatible releases.

Status, progress, warnings, and errors are written to stderr. JSON output is
written to stdout.

## Top-level examples

```sh
sigil op "," "find large files"
sigil op "??" "what changed in this repo?"
sigil op "^^" "generate a cleanup patch"
sigil op --dry-run ",,," "clean build outputs"
sigil patch check
sigil patch apply --yes
sigil install zsh
sigil doctor
sigil events lineage
sigil session show --json
```

## `sigil op --json`

Parses a glyph invocation without running the model. This is the stable
machine-readable introspection path for shell bindings and tests.

```json
{
  "glyph": "??",
  "base": "?",
  "depth": 2,
  "name": "inspect",
  "prompt": "review risky changes",
  "stdin": "diff --git a/file b/file\n",
  "mode": "pipeline"
}
```

Stable fields:

- `glyph`
- `base`: the operator family, currently `?`, `,`, or `^`.
- `depth`: repeated-glyph count.
- `name`: semantic operator name.
- `prompt`: user prompt text after the glyph.
- `stdin`: captured input stream.
- `mode`: `pipeline` or `interactive`.

Without `--json`, `sigil op` runs the operator. Piped `?` / `??` inspect stdin,
`,` / `,,` synthesize or propose output, and `^` / `^^` generate repair
previews. Operator output is written to stdout; status and errors go to stderr.

Depth-3 operators are treated as higher-autonomy requests and pass through the
execution policy gate:

```sh
sigil op ",,," "find and remove generated files"
sigil op --dry-run ",,," "find and remove generated files"
sigil op --yes --policy allow ",,," "find and remove generated files"
```

Current policy behavior:

- default depth-3 output is blocked after preview; no commands are run.
- `--dry-run` classifies the output and exits successfully without execution.
- `--yes --policy allow` acknowledges the gate, but execution is not implemented
  yet; Sigil still emits a preview only.

The policy classifier records broad action classes such as `execute`,
`file_write`, `network`, `delete`, and `privileged` in the event log.

## `sigil patch`

Repair operators store unified diffs as the current session's patch preview.
Patch application is separate from `^` / `^^` so model output remains visible
before any file write.

```sh
sigil patch show
sigil patch check
sigil patch apply --yes
```

`show` prints the stored diff. `check` runs `git apply --check` in the working
directory where the preview was created. `apply` requires `--yes` and then runs
`git apply`; without `--yes`, it exits with status `2` and does not modify
files.

The JSON form is available for every subcommand:

```sh
sigil patch show --json
sigil patch check --json
sigil patch apply --yes --json
```

`check` and `apply` record audit events with the patch event id, command,
status, cwd, and bounded stdout/stderr snippets.

## `sigil events lineage --json`

Inspects the read-only global event log and returns the selected event plus the
transitive input events it inherited from.

```sh
sigil events lineage
sigil events lineage <event-id>
sigil events lineage <event-id> --json
```

When no event id is provided, Sigil uses the latest event from the current
session, falling back to the latest global event. The JSON form returns:

- `event_id`: selected event id.
- `nodes`: ordered lineage nodes starting with the selected event.
- `nodes[].depth`: distance from the selected event.
- `nodes[].event`: normalized event payload with trust metadata.
- `missing_inputs`: input ids referenced by events but absent from the log.

## `sigil session --json`

`session` has four JSON forms:

```sh
sigil session show --json
sigil session path --json
sigil session list --json
sigil session clear --json
```

`show` returns the current session id, path, and parsed continuity files.
`path` returns the global state path, current session path, session id, and
global event log path. `list` returns known session directories and the files
present in each. `clear` returns the session files removed.

## Hidden plumbing commands

These commands are intentionally not shown in top-level help and are not stable
user-facing API:

- `sigil render-pi-stream`
- `sigil record-failure`

They exist so shell bindings can keep a small, explicit boundary with the Python
runtime.

`record-failure` accepts optional `--stdout-snippet` and `--stderr-snippet`
fields. The passive shell hooks cannot safely capture arbitrary command output
by themselves, but terminal wrappers or future execution sandboxes can pass
bounded snippets through this stable hidden boundary.

## `sigil install`

Installs or updates a shell binding from the installed Sigil package and adds an
idempotent source block to the shell rc file.

```sh
sigil install zsh
sigil install bash
```

Useful options:

```sh
sigil install bash --install-dir ~/.sigil/shell/bash --rc ~/.bashrc
sigil install zsh --json
```

The JSON form reports:

- `shell`: installed shell binding.
- `binding_path`: binding file written by Sigil.
- `rc_path`: rc file inspected or updated.
- `source_path`: bundled binding source copied from.
- `wrote_rc`: whether Sigil appended a source block.

## `sigil doctor`

Checks local install readiness:

```sh
sigil doctor
sigil doctor --shell bash
sigil doctor --json
```

Doctor checks:

- `sigil`, `fzf`, `glow`, and `pi` are on `PATH`.
- the local model endpoint is reachable from `QWEN_URL`, or the default local
  endpoint.
- `QWEN_MODEL` is set when the endpoint needs an explicit model name.
- Sigil's state directory is writable.
- the selected shell is supported.
- the selected shell binding is installed.
- the current environment looks like it inherited a loaded binding.

The JSON form returns an ordered list of checks with `name`, `status`, `detail`,
and optional `hint`. `doctor` exits nonzero when any check has `status: "fail"`;
warnings do not change the exit code.
