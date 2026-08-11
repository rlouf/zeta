# zeta-connectors

The bundled Zeta event connectors. They turn external activity into durable
events and deliver requested effects through the IPC protocol. Installing this
package puts `zeta-connector-<id>` commands on `PATH`, which is all the
registration a Zeta runtime needs. Discovery is the shell's.

- `zeta-connector-filesystem` — watches directories, emits
  `file.created`.
- `zeta-connector-slack` — Slack over Socket Mode ingress and
  `slack.message.post` egress. Install the `slack` extra
  (`zeta-connectors[slack]`) for the websocket dependency; secrets:
  `SLACK_APP_TOKEN` (socket), `SLACK_BOT_TOKEN` (egress).
- `zeta-connector-telegram` — Telegram long-poll ingress with
  ack-confirmed offsets and `telegram.message.send` egress; secrets:
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_SENDERS`.
- `zeta-connector-pushover` — pure egress: `pushover.message.send`
  and `pushover.glance.update`; secrets: `PUSHOVER_API_TOKEN`,
  `PUSHOVER_USER_KEY`.

Every executable answers `--describe` with its static JSON manifest: events,
filter schemas, and operations with delivery semantics. With no arguments, it
initializes as an IPC source, provider, or both over standard input and output.
Losing an upstream connection exits the process. The runtime supervisor starts
it again.
