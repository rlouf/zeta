# Zeta

Zeta is an operating substrate for agents. It provides the
`zeta` CLI, agent manifests, event stores, model adapters, prompt construction,
capability execution, and runtime services.

## Tool executors

The runtime loop stays in Zeta. An agent can select where its tool calls run
through frontmatter:

```yaml
executor:
  provider: local
  config: {}
```

`local` runs registered capabilities in the current process. Additional
providers are installed through the `zeta.tool_executors` entry-point group.
Zeta sets up one executor per provider, agent, and configuration, then reuses
it across invocations while keeping the model loop local.
Provider setup is asynchronous. Returned executors must support concurrent
calls; Zeta awaits their `aclose()` method during worker shutdown.

Executor configuration is persisted in project snapshots and execution
manifests. Store only secret references, profile names, or environment-variable
names there—never credentials or secret values. Providers resolve references
during setup. Config must contain JSON-compatible values with string object
keys.
