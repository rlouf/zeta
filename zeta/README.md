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
providers are installed through the `zeta.tool_executors` entry-point group;
each provider receives the agent id, capability registry, and `config` mapping
for the invocation.
