# Sigil

Punctuation-native LLM interaction for the shell.

Sigil is structured as a shell-agnostic core with thin shell bindings. The zsh
layer owns prompt interception and buffer insertion; the executable owns model
calls, selection UI, Pi streaming, rendering, and persistent state.

## Grammar

```text
,   generate shell command candidates
,,  reopen the previous command selector
?   answer a question with Pi using read + web search
```

## Layout

```text
bin/sigil              shell-agnostic CLI
bin/stream-pi-json     Pi JSON event filter
sigil/                 Python core runtime
zsh/sigil.zsh          zsh bindings only
```

Core commands:

```sh
sigil command --select "find wav files"
sigil previous-command --select
sigil question "what is tldraw?"
sigil stream-pi-json
```

The zsh binding calls those commands and inserts selected commands back into the
prompt with `print -z`.

## State

Sigil writes state under:

```text
${XDG_STATE_HOME:-~/.local/state}/sigil/
```

Current files:

```text
events.jsonl        append-only interaction/tool/answer event log
last-command.json   latest generated command candidates for `,,`
```

The event log is the durable substrate for future `??`, `@.`, `@@`, and `!!`
behavior. Shell globals are intentionally not used for session continuity.

## zsh

Source the zsh entrypoint from `.zshrc`:

```zsh
source "$HOME/projects/sigil/zsh/sigil.zsh"
```

## Requirements

- `python3`
- `curl`-compatible local llama.cpp/OpenAI endpoint for command generation
- `fzf` for command selection
- `glow` for Markdown rendering
- `pi` for question answering

Environment knobs:

```sh
QWEN_URL=http://127.0.0.1:8080/v1/chat/completions
QWEN_MODEL=qwen3.6-27b-q8-local
```
