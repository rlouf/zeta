# Zeta Plugin SDK

The Zeta Plugin SDK declares Python model, tool, connector, and executor
providers.
The Zeta host loads declarations and calls providers through a private protocol.
The SDK does not run agents.

## Declare project providers

A project does not need to be a Python package. Put provider modules in these
directories at the project root:

```text
agents/
models/
connectors/
tools/
```

Use decorators to declare providers:

```python
from zeta_plugin import connector, model, tool


@tool(
    "pi.bash",
    input_schema={
        "type": "object",
        "required": ["command"],
        "properties": {"command": {"type": "string"}},
    },
)
async def bash(request, context):
    """Run a command in the Pi workspace."""
    return {"output": "ok"}


@model("codex", tool_profile={"pi.bash": "bash"})
async def generate(request, context):
    return {"message": {"role": "assistant", "content": []}}


@connector("slack")
class Slack:
    async def deliver(self, request, context):
        return {"delivery_id": "123"}
```

A model or tool receives `request` and `context`. A connector class must define
`deliver`, `subscribe`, or both. An executor class must define `open`, `call`,
and `close`. Zeta creates one class instance per host. Raise `ProviderError` to
set a stable error code and its retryable state.

Each tool must have a model description. The SDK uses its first docstring
paragraph by default. Use `@tool(..., description="...")` or
`zeta.tools.register(..., description="...")` to override it. An undescribed
tool fails during provider registration.

Each tool must also have an `input_schema` object. Zeta sends the schema to
the model. It does not infer a schema or use a permissive default.

A subscription receives an event type, filter, and optional cursor. It returns
an `events` array of payload objects and an optional replacement cursor.

## Select a connector

An agent selects an egress connector in its `publishes` declaration:

```markdown
---
name: Announcer
description: Sends one Slack message.
accepts: [release.ready]
publishes:
  - event: slack.message.post
    connector: slack
    with:
      channel: C123
---
Call `publish_event` with the message payload.
```

Zeta calls `Slack.deliver` after it persists the published event. The connector
gets an operation, payload, options, and idempotency key. The host also gives
the connector the stable effect key in `context`.

For inbound events, select the connector from `accepts`:

```yaml
accepts:
  - event: slack.message.received
    connector: slack
    filter:
      channel_ids: [C123]
    idempotency_key: "slack:{message_ts}"
```

## Ship a provider distribution

A Python distribution can expose an entry point in the `zeta.providers` group:

```toml
[project.entry-points."zeta.providers"]
pi = "pi_zeta.providers:setup"
```

The entry point can export decorated providers or a `setup(zeta)` function.
The setup object has `models`, `tools`, `connectors`, and `executors`
registration APIs:

```python
def setup(zeta):
    zeta.tools.register(
        "pi.bash",
        bash,
        description="Run a command in the Pi workspace.",
        input_schema={"type": "object", "required": ["command"]},
    )
    zeta.models.register("pi", generate, tool_profile={"pi.bash": "bash"})
    zeta.connectors.register("slack", Slack)
    zeta.executors.register("modal", ModalExecutor)
```

Project providers take priority over distribution providers with the same
category and identifier.
