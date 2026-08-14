# Python providers

Zeta can load model, tool, and connector providers from Python.

Install the `zeta-plugin` SDK in the project environment. The runtime starts
one private Python host for each project revision. It records the provider
catalog and source fingerprints in the revision.

## Local providers

A project does not need to be a Python package. Put modules in these root
directories:

```text
agents/
models/
connectors/
tools/
```

Each module uses one provider decorator. This example exposes a Pi shell tool:

```python
from zeta_plugin import tool


@tool("pi.bash")
async def bash(request, context):
    return {"output": run_bash(request["command"])}
```

Model providers use `@model`. Their optional `tool_profile` maps canonical tool
identifiers to tool names for that model.

```python
from zeta_plugin import model


@model("pi", tool_profile={"pi.bash": "bash"})
async def generate(request, context):
    return {"message": {"role": "assistant", "content": []}}
```

Tool identifiers can be simple names such as `read`. A package can use its own
namespace, such as `pi.bash`. Do not use `zeta` as a tool namespace.

## Connector delivery

Use `@connector` on a class that defines `deliver`, `subscribe`, or both.

```python
from zeta_plugin import connector


@connector("slack")
class Slack:
    async def deliver(self, request, context):
        return {"delivery_id": send(request["payload"])}

    async def subscribe(self, request, context):
        return {"events": [], "cursor": request.get("cursor")}
```

Select delivery from an agent. Add `connector` to the relevant `publishes`
entry:

```markdown
---
name: Announcer
description: Sends a Slack message.
accepts: [release.ready]
publishes:
  - event: slack.message.post
    connector: slack
    with:
      channel: C123
---
Use `publish_event` to send the message.
```

Zeta writes the event before it calls `deliver`. It gives the connector an
operation, payload, options, and idempotency key. It also gives the stable
effect key in `context`. The runtime retries failed delivery under its normal
effect policy.

Use `connector` in `accepts` to select a subscription:

```markdown
accepts:
  - event: slack.message.received
    connector: slack
    filter:
      channel_ids: [C123]
    idempotency_key: "slack:{message_ts}"
```

Zeta calls `subscribe` once per binding. The request contains `event_type`,
`filter`, and the last cursor when one exists. Return an `events` array of
payload objects. Return `cursor` when the connector can resume from that
position. Zeta stores the cursor only after it stores every event in the batch.

Raise `ProviderError` when a provider must return a stable failure code. Set
`retryable=True` when the connector can safely retry the same request.

## Provider distributions

Provider packages use the `zeta.providers` Python entry point group. This
example belongs in the package `pyproject.toml`:

```toml
[project.entry-points."zeta.providers"]
pi = "pi_zeta.providers:setup"
```

The entry point can export decorated targets, a `providers(...)` collection, or
a `setup(zeta)` function. The setup function receives separate registration
APIs:

```python
def setup(zeta):
    zeta.models.register("pi", generate, tool_profile={"pi.bash": "bash"})
    zeta.tools.register("pi.bash", bash)
    zeta.connectors.register("slack", Slack)
```

Install the distribution into the project Python environment. Its providers
then become available during `zeta check` and project reload. Local providers
override a package provider with the same category and identifier.
