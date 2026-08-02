# Model-Specific Tool Profiles

## Purpose

Models do not all learn the same tool interface. A model can select a wrong
tool name or use wrong arguments when the interface is different from its
training interface.

A tool profile gives the model the interface that it expects. The profile can
change a tool name, description, and input schema. It can also adapt the model
arguments. It does not change the capability implementation.

This separation keeps two contracts:

- The model contract can match the model training.
- The canonical contract stays stable for Zeta and its executors.

## Terms

A **canonical capability** has a stable ID, input schema, delivery rule, and
executor. For example, `zeta.bash` is a canonical capability.

A **tool presentation** has the name, description, and input schema that the
model sees. It can also have an argument adapter.

A **tool profile** selects presentations for a model. A capability uses its
native presentation when the profile has no override for it.

## Execution Flow

Zeta uses this flow for each model tool call:

```text
model profile
  -> tool profile
  -> ordered tool descriptors
  -> model tool call
  -> model argument validation
  -> argument adapter
  -> canonical argument validation
  -> canonical executor
```

Zeta validates the model arguments before it uses the adapter. It validates
the canonical arguments after it uses the adapter. The first check finds a bad
model call. The second check stops a bad profile from sending unchecked data
to an executor.

Zeta uses the canonical capability ID and canonical arguments for effect
records. As a result, the same operation has the same effect identity with all
tool profiles.

## Built-In Profiles

The `native` profile uses the native presentation for each capability.

The `codex` profile has these overrides:

| Canonical capability | Model name | Model arguments | Canonical arguments |
| --- | --- | --- | --- |
| `zeta.bash` | `exec_command` | `{"cmd": "..."}` | `{"command": "..."}` |
| `zeta.patch` | `apply_patch` | `{"patch": "..."}` | `{"patch": "..."}` |

All other capabilities keep their native presentation. The built-in Codex
model selects the `codex` profile.

## Configuration

Set `tool_profile` in a model profile:

```toml
[[models]]
name = "codex"
model = "gpt-5.6-sol"
api = "codex-responses"
tool_profile = "codex"
```

Zeta uses `native` when the field is not present. Zeta does not infer a tool
profile from the model name. This rule makes the model configuration explicit
and easy to inspect.

## Edit And Patch Capabilities

`zeta.edit` changes one file. It uses an exact replacement or hashline
operations. Exact matching stops an edit when the old content is missing or
is not unique.

`zeta.patch` can add, update, move, or delete files. It accepts the Codex patch
format between `*** Begin Patch` and `*** End Patch` markers. It checks every
section before it changes a file. This check prevents an invalid later section
from causing a partial patch.

Patch paths must be relative to the base directory. A path cannot be absolute
and cannot contain `..`. Update context must have one exact match. These rules
make patch results clear and repeatable.

The `codex` profile presents `zeta.patch` as `apply_patch`. The executor still
runs the canonical `zeta.patch` capability.

## Trace And Replay

Zeta stores the exact ordered tool descriptors with the prompt trace. Replay
uses the stored descriptors. It does not build new descriptors from the
current model configuration.

This rule is necessary because a profile can change after a run. Replay must
show the model the same names, descriptions, schemas, and order that the
original run used.
