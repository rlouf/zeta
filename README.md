# Sigil

Punctuation-native LLM interaction for the shell.

Status: this is currently a "works on my machine" repo. If you are interested
in an easier-to-install version, please open an issue.

Sigil is structured as a shell-agnostic core with thin shell bindings. The zsh
layer owns prompt interception and buffer insertion; the Python CLI owns model
calls, selection UI, Pi streaming, rendering, and persistent state.

## Grammar

```text
,   generate shell command candidates
,,  reopen the previous command selector
^   suggest fixes for the last failed command
^^  reopen previous fix candidates
?   answer a question with Pi using read + web search
??  continue the previous question discussion
```

Sigil records every glyph invocation with trust metadata. The current grammar
maps to:

```text
,   human prompt -> local model proposal   local_model / propose / model-tainted
,,  command continuation                   inherits prior command taint
?   read + web question                    web / read / web-tainted / provisional
??  question continuation                  inherits prior question taint / provisional
```

This matters because Sigil crosses the shell boundary by inserting text into the
prompt. Model-authored command suggestions are proposals, not executed actions.
Web-tainted question answers are read-only and provisional, and cannot become an
executable insertion path through `??`.

The full trust model is documented in
[docs/security-lattice.md](docs/security-lattice.md).

## Install

Current rough install:

```sh
uv tool install git+https://github.com/rlouf/sigil
curl -fsSL https://raw.githubusercontent.com/rlouf/sigil/main/scripts/install.zsh | zsh
```

The installer downloads the zsh binding to `~/.sigil/shell/zsh/sigil.zsh` and
adds an idempotent source block to `~/.zshrc`.

## Layout

```text
scripts/install.zsh    zsh binding installer
sigil/                 Python core runtime
zsh/sigil.zsh          zsh bindings only
```

Core commands:

```sh
sigil command --select "find wav files"
sigil previous-command --select
sigil fix
sigil previous-fix
sigil question "what is tldraw?"
sigil follow-up "how would that work in practice?"
sigil session show
sigil session path
sigil session list
sigil session clear
sigil stream-pi-json
```

The zsh binding calls those commands and inserts selected commands back into the
prompt with `print -z`.

## State

Sigil writes state under:

```text
~/.sigil/
```

Current files:

```text
events.jsonl                                 append-only global event log
sessions/<session-id>/last-command.json      latest command candidates for `,,`
sessions/<session-id>/last-failure.json      latest failed shell command
sessions/<session-id>/last-fix.json          latest fix candidates for `^^`
sessions/<session-id>/last-question.jsonl    question transcript; reset by `?`
sessions/<session-id>/last-tools.jsonl       latest Pi tool trace
```

Events and session JSONL entries include these trust fields:

```json
{
  "glyph": "?",
  "inputs": ["event-id"],
  "integrity": "web",
  "capability": "read",
  "taint": ["web"],
  "provisional": true
}
```

Legacy state that predates those fields is treated as low-trust:
`integrity=unknown`, `capability=none`, and `taint=["legacy"]`.

The event log is the durable substrate for future `@.`, `@@`, and `!!`
behavior. Shell globals are intentionally not used for session continuity.

## zsh

Source the zsh entrypoint from `.zshrc`:

```zsh
source "$HOME/.sigil/shell/zsh/sigil.zsh"
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
QWEN_MODEL_PATH=/path/to/model.gguf
```
