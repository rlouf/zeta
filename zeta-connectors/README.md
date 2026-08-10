# zeta-connectors

The bundled Zeta event connectors, packaged as executables that speak
`wire-v0` (see `spec/wire-v0.md` in the zeta repository). Installing
this package puts `zeta-connector-<id>` commands on `PATH`, which is
all the registration a Zeta runtime needs — discovery is the shell's.

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

Every executable answers `--describe` with its static JSON manifest
(events, filter schemas, operations with delivery semantics) and
speaks the protocol on stdio when run without arguments. Connectors
never reconnect on their own: losing an upstream connection exits the
process, and the runtime's supervisor respawn is the reconnect logic.
