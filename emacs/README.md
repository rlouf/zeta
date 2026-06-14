# Zeta Block for Emacs

`zeta-block.el` is a small Emacs frontend for the Zeta JSON-RPC runtime. It lets
you write a question block in any buffer, press `C-c C-c`, and insert Zeta's
answer below the block.

```markdown
? Who are you?

Zeta:
  I am Zeta, answering through the local Sigil/Zeta runtime.
```

In source buffers, comment blocks stay commented:

```python
# ? Is this branch still needed?
#
# Zeta:
#   Probably not. The branch looks specific to the old shell handoff path.
```

The frontend starts `sigil zeta rpc --stdio`, registers an `emacs_read`
read-only tool for the current live buffer, and runs `session.run` with the
read-only ask workflow. It does not register edit/write tools and does not apply
changes to buffers.

## Doom Emacs Install

Add the local package to `~/.doom.d/config.el`:

```elisp
;; Zeta block submitter: C-c C-c on a ? block asks the local Zeta RPC backend.
(use-package! zeta-block
  :load-path "/Users/remilouf/projects/sigil/emacs"
  :demand t
  :config
  (setq zeta-block-sigil-command "/Users/remilouf/projects/sigil/.venv/bin/sigil")
  (zeta-block-global-mode 1))
```

Then reload Doom or restart Emacs:

```elisp
M-x doom/reload
```

For a single-session reload while developing the package:

```elisp
M-x load-file
/Users/remilouf/projects/sigil/emacs/zeta-block.el
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

The mode line shows the subprocess status:

```text
Zeta:off
Zeta:idle
Zeta:run
Zeta:err
```

Use `M-x zeta-block-status` for details such as the process id, active request
count, or last error.
