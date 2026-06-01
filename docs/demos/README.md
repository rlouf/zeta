# Sigil VHS Demos

This directory contains deterministic VHS recordings for product demos.

The tapes in `docs/demos/tapes/` run the real Sigil CLI from this checkout.
They use deterministic shims only for external dependencies so GIFs can be
regenerated without a live model or `zeta`. The lazygit demo uses real `lazygit`.

## Render

```sh
scripts/render-demo-gifs.sh
```

Render one or more tapes:

```sh
scripts/render-demo-gifs.sh docs/demos/tapes/10-lazygit-review.tape
```

List available tapes without rendering:

```sh
scripts/render-demo-gifs.sh --list
```

GIFs are written to `docs/demos/gifs/`.

## What The Shim Does

`setup.zsh` creates a temporary Git repo and prepends a temporary `bin/` to
`PATH`. The `sigil` command in that bin invokes `python3 -m sigil.cli` from the
checkout. The fake model server, `zeta`, and `uv` commands are small Python
programs in this directory. They make model output stable while Sigil itself
still owns routing, shell glyphs, state, act state, and the event log.

This keeps the recordings focused on the workflow:

- shell glyphs over a boring CLI
- normal Git and lazygit in the middle of the flow
- read-only question routes
- explicit proposal, execution, and act boundaries
- inspectable event history
