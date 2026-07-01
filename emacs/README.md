# Zeta Block for Emacs

`zeta-block.el` is a small Emacs frontend for the Zeta JSON-RPC runtime. It
supports two editor-native workflows:

- Write a question block in any buffer, press `C-c C-c`, and insert Zeta's
  answer below the block.
- Write an inline prompt beginning with `zeta?` or `zeta!`, press `RET`, and let
  Zeta work against the live buffer while you keep editing.

```markdown
? Who are you?

Zeta:
  I am Zeta, answering through the local Commas/Zeta runtime.
```

In source buffers, comment blocks stay commented:

```python
# ? Is this branch still needed?
#
# Zeta:
#   Probably not. The branch looks specific to the old shell handoff path.
```

The frontend starts the command in `zeta-block-rpc-command`, registers live
buffer tools, and runs `session.run`. Question blocks and `zeta?` prompts ask
for a final answer, which Emacs inserts below the prompt. Inline `zeta!`
instructions are allowed to act on the document with an `emacs_replace` tool
that only replaces a line range when the current buffer text still matches what
the agent read.

## Install

Prerequisites:

- The Zeta checkout is available locally.
- The `zeta` executable can run `zeta rpc --stdio`.

If `zeta` is already on `PATH`, the default command is enough:

```elisp
(setq zeta-block-rpc-command '("zeta" "rpc" "--stdio"))
```

If you are developing against this checkout, point Emacs at the local package
and executable.

### Vanilla Emacs

Add this to your Emacs config:

```elisp
(add-to-list 'load-path "/Users/remilouf/projects/zeta/emacs")
(require 'zeta-block)

(setq zeta-block-rpc-command
      '("/Users/remilouf/projects/zeta/.venv/bin/zeta" "rpc" "--stdio"))

(zeta-block-global-mode 1)
```

Then reload the config or evaluate the forms.

### Doom Emacs

Add the local package to `~/.doom.d/config.el`:

```elisp
;; Zeta block submitter: C-c C-c on a ? block asks the local Zeta RPC backend.
(use-package! zeta-block
  :load-path "/Users/remilouf/projects/zeta/emacs"
  :demand t
  :config
  (setq zeta-block-rpc-command
        '("/Users/remilouf/projects/zeta/.venv/bin/zeta" "rpc" "--stdio"))
  (zeta-block-global-mode 1))
```

Then reload Doom or restart Emacs:

```elisp
M-x doom/reload
```

### Development Reload

For a single-session reload while developing the package:

```elisp
M-x load-file
/Users/remilouf/projects/zeta/emacs/zeta-block.el
M-x zeta-block-restart
```

## Usage

Enable the mode globally with the Doom stanza above, or manually:

```elisp
M-x zeta-block-global-mode
```

Write a block whose cleaned text starts with `?`, then press `C-c C-c` while
point is inside the block.

If the current block does not start with `?`, `zeta-block-mode` falls through to
the original `C-c C-c` binding for the active major mode.

For inline questions, use `zeta?`:

```markdown
zeta? Is the previous paragraph clear?
```

For inline edits or actions, use `zeta!`:

```markdown
zeta! Correct typos in previous paragraph
```

The `zeta?` and `zeta!` trigger words are highlighted in bold using faces
derived from the active Emacs theme. Questions inherit from
`font-lock-keyword-face`; actions inherit from `font-lock-warning-face`.

The normal return command runs first, then Zeta starts in the background. The
mode line switches to `Zeta:run`, a temporary response is inserted under the
instruction, and you can keep working. For `zeta?`, the model can inspect the
live buffer and other allowed context, but Emacs only inserts the final answer
below the prompt. For `zeta!`, if the buffer changes under the target line range
before the agent edits it, the edit is rejected and the agent must read again
instead of overwriting your new text.

Inline prompts and region commands are queued per buffer. Zeta runs one queued
task at a time, so you can keep adding `zeta?` and `zeta!` prompts while the
current task is still running. Use:

```text
C-c z q   show this buffer's queue status
C-c z p   pause or resume this buffer's pairing queue
```

For scoped region work, select text and use:

```text
C-c z ?   ask about the selected region
C-c z !   edit or act on the selected region
```

Both commands prompt in the minibuffer. Region questions insert an answer below
the selected region. Region actions tell Zeta to edit only the selected line
range, while still allowing surrounding context for style and meaning.

Submitted prompts and Zeta-authored responses/edits are tagged with Emacs
overlays carrying `zeta-origin` and `zeta-prompt` properties. Hovering shows the
origin and prompt. Use `M-x zeta-block-clear-overlays` to remove the visual tags.

The mode line shows the subprocess status:

```text
Zeta:off
Zeta:idle
Zeta:run
Zeta:pair
Zeta:queue
Zeta:pause
Zeta:err
```

Use `M-x zeta-block-status` for details such as the process id, active request
count, or last error.
