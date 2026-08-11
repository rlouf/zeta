# zeta-connectors

The bundled Zeta event connectors. They turn external activity into durable
events and deliver requested effects through the IPC protocol.

Install the package in Zeta's active Python environment:

```sh
uv pip install zeta-connectors
```

This registers four plugins under the `zeta.event_connectors` entry-point
group:

- `filesystem` watches directories and emits `file.created`.
- `slack` provides Slack Socket Mode ingress and `slack.message.post` egress.
  Install `zeta-connectors[slack]` for the websocket dependency. It uses
  `SLACK_APP_TOKEN` for the socket and `SLACK_BOT_TOKEN` for egress.
- `telegram` provides long-poll ingress with acknowledged offsets and
  `telegram.message.send` egress. It uses `TELEGRAM_BOT_TOKEN` and
  `TELEGRAM_ALLOWED_SENDERS`.
- `pushover` provides `pushover.message.send` and
  `pushover.glance.update` egress. It uses `PUSHOVER_API_TOKEN` and
  `PUSHOVER_USER_KEY`.

The Python frontend turns each registration into a connector id and an
argument vector. It starts the entry-point target in a child process. With
`--describe`, the target returns its static JSON manifest: events, filter
schemas, and operations with delivery semantics. With no arguments, it
initializes as an IPC source, provider, or both over standard input and output.
The runtime supervises the process and starts it again after a failed upstream
connection.
