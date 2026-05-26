# Sigil

Verb-first LLM interaction for the shell, with optional punctuation shortcuts.

![15-second Sigil terminal demo](docs/demo.gif)

Status: this is currently a "works on my machine" repo. If you are interested
in an easier-to-install version, please open an issue.

The Python package is named `sigil-sh` because `sigil` was not available as a
distribution name. The installed command is still `sigil`, and this repository
uses `sigil` everywhere else.

Sigil is structured as a shell-agnostic core with thin shell bindings. The shell
layer owns glyph dispatch; the Python CLI owns model calls, selection UI, Pi
streaming, rendering, and persistent state.

## Commands

```text
sigil command "find wav files"              generate command candidates
sigil fix                                   suggest fixes for the last failure
sigil op "^"                                repair from stdin or last failure
sigil op "^^"                               deeper repair pass
sigil ask "what changed in this repo?"      answer a question with Pi
sigil ask --follow-up "what should I run?"  continue the prior answer
```

Piped stdin is first-class:

```sh
git diff | sigil ask "review risky changes"
cat notes.md | sigil command "turn this into a release command"
printf '%s\n' src/sigil/cli.py | sigil fix "preview a small cleanup"
```

## Optional Glyphs

Glyphs are a shortcut layer on top of the CLI runtime. Installed shell bindings
enable them by default. Use `sigil install <shell> --no-glyphs` if you only want
the long-form verbs.

```text
,   -> sigil op ","
,,  -> sigil op ",,"
^   -> sigil op "^"
^^  -> sigil op "^^"
?   -> sigil ask
??  -> sigil ask --follow-up
```

Sigil records every invocation with trust metadata. This is the core trust
lattice:

```text
integrity:  human > local_model > local_file > web > unknown
capability: none < propose < read < write_boxed < exec_boxed
taint:      model, web, legacy
```

The default glyph aliases map to:

```text
,   human prompt -> model recommendation   local_model / propose / model-tainted
,,  human prompt -> generated command run  local_model / exec_boxed / model-tainted
^   failed command/files -> repair preview  local_model / propose / model-tainted
^^  failed command/files -> deeper repair   local_model / propose / model-tainted
?   read + web question                    web / read / web-tainted / provisional
??  question continuation                  inherits prior question taint / provisional
```

This matters because only the explicit comma execution route crosses into
`exec_boxed`. Web-tainted question answers are read-only and provisional, and
cannot become an executable proposal path through `??`.

Current no-execute guarantees:

```text
no ?! parser route
no promotion mutation
```

The full trust model is documented in
[docs/security-lattice.md](docs/security-lattice.md).

## Install

Install the Python command, then install the shell binding you use:

```sh
uv tool install git+https://github.com/rlouf/sigil
sigil install zsh
sigil doctor
```

To install without punctuation shortcuts:

```sh
sigil install zsh --no-glyphs
```

For Bash:

```sh
uv tool install git+https://github.com/rlouf/sigil
sigil install bash
sigil doctor --shell bash
```

`install` copies the bundled binding to `~/.sigil/shell/<shell>/` and adds an
idempotent source block to `.zshrc` or `.bashrc`. Running it again updates the
binding without duplicating the rc block.

`sigil doctor` checks the local pieces:

```sh
sigil
glow
pi
QWEN_URL / local model endpoint
QWEN_MODEL
state directory writability
shell support
installed shell binding
loaded shell binding
```

The endpoint check is expected to warn unless your local OpenAI-compatible model
server is already running.

## Layout

```text
shell/bash/sigil.bash    Bash binding
shell/zsh/sigil.zsh    zsh binding
src/sigil/             Python core runtime
```

Core commands:

```sh
sigil command --select "find wav files"
sigil op "," "recommend next cleanup"
sigil op ",," "run the relevant tests"
sigil op "^"
sigil op "^^"
sigil ask "what is tldraw?"
sigil ask --follow-up "how would that work in practice?"
sigil op --dry-run ",," "clean build outputs"
sigil patch show
sigil patch check
sigil patch apply --yes
sigil install zsh
sigil doctor
sigil events lineage
sigil session show
sigil session path
sigil session list
sigil session clear
```

The shell bindings call `sigil op` for glyph behavior. `,` prints one
recommended command with an explanation and adds the command to shell history;
`,,` asks for one shell command and executes it. When either comma route receives
piped input, Sigil previews that input and asks for confirmation before using it;
piped `,,` also asks before executing the generated command.

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
sessions/<session-id>/last-patch.json        latest repair patch preview
sessions/<session-id>/last-question.jsonl    question transcript; reset by `?`
sessions/<session-id>/last-tools.jsonl       latest Pi tool trace
```

Failure records include command, status, cwd, safe cwd/git context, and optional
bounded stdout/stderr snippets when a wrapper provides them. Fix suggestions
show their rationale on stderr or in the selector, while stdout remains only the
selected command.

Repair operators that emit a unified diff store it as the current patch preview.
`sigil patch show` prints that preview, `sigil patch check` validates it with
`git apply --check`, and `sigil patch apply --yes` applies it explicitly with
`git apply`.

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

The event log is the durable substrate for session continuity. Shell globals are
intentionally not used for that state.

## zsh

Source the zsh entrypoint from `.zshrc`:

```zsh
source "$HOME/.sigil/shell/zsh/sigil.zsh"
```

## Bash

Source the Bash entrypoint from `.bashrc`:

```bash
source "$HOME/.sigil/shell/bash/sigil.bash"
```

Use the `sigil command`, `sigil ask`, and `sigil fix` verbs directly in Bash.
When glyphs are enabled, Bash also supports:

```bash
, find wav files
,, run the relevant tests
? what is tldraw?
?? how would that work in practice?
^
^^
```

`,` prints a recommended command plus explanation and adds the command to shell
history. Non-piped `,,` executes the generated command immediately. Piped comma
routes ask before using the input, and piped `,,` asks again before execution.
`^` and `^^` print repair previews.

## Requirements

- `python3`
- `curl`-compatible local llama.cpp/OpenAI endpoint for command generation
- `fzf` optionally improves `sigil command --select`; a built-in selector is used
  when it is unavailable
- `glow` for Markdown rendering
- `pi` for question answering

`pi` is the .txt agent CLI used by the `?` and `??` routes. It is not installed
by Sigil. Install and configure it separately, then verify `pi --help` works.
Sigil invokes it as `pi --json --tools read,web_search ...`, then renders the
event stream through `sigil render-pi-stream` so tool calls, answer text, and
trust metadata are recorded in Sigil state. `pi` must be on `PATH`, and for the
current local setup it should be able to start or reach the same Qwen endpoint
used by Sigil.

Environment knobs:

```sh
QWEN_URL=http://127.0.0.1:8080/v1/chat/completions
QWEN_MODEL=qwen3.6-27b-q8-local
QWEN_MODEL_PATH=/path/to/model.gguf
SIGIL_GLOW_STYLE=notty
SIGIL_GLOW_WIDTH=88
```

By convention this repo expects the helper script
`~/.config/pi/run-qwen36-q8.sh` to start a llama.cpp-compatible server on
`127.0.0.1:8080`. You can also run `llama-server` yourself with the same alias
and port.
