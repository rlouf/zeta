# Zeta Plugin SDK

The Zeta Plugin SDK declares Python model, tool, and connector providers.

The SDK does not run agents. The Zeta host loads the declarations and invokes
the provider through its private host protocol.

```python
from zeta_plugin import tool


@tool("pi.bash")
async def bash(request, context):
    return {"output": "ok"}
```

A project can put modules in `models/`, `tools/`, and `connectors/`.
Zeta loads those modules during provider discovery. A distribution can later
expose the same declarations through the `zeta.providers` entry point group.
