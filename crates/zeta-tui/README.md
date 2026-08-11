# zeta-tui

`zeta-tui` is an interactive terminal program for agent sessions.
It makes session activity easy to follow and control from a terminal.
It shows sessions, conversation output, and run progress.
It can send messages to new or existing sessions.

## Contents

- A session list and a conversation timeline.
- Formatted Markdown and compact tool activity.
- A raw event view.
- Session search and message input.
- Automatic reconnection after a transport failure.

## Run

Run the program with the default RPC executable:

```sh
cargo run -p zeta-tui
```

Or provide the path to an RPC executable:

```sh
cargo run -p zeta-tui -- /absolute/path/to/rpc-program
```

The RPC executable must accept `rpc stdio`.
When no path is present, the program uses the default RPC executable from `PATH`.

## Main keys

| Key | Action |
|---|---|
| `n` | Start a new session. |
| `Enter` | Open the selected session or send a message. |
| `i` | Write a message. |
| `/` | Search for a session. |
| `Esc` | Return to the session list. |
| `?` | Show all keys. |
| `q` | Quit. |

## Test

```sh
cargo test -p zeta-tui
```
