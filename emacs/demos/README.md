# Zeta Emacs Demos

These screencasts are deterministic terminal recordings rendered from
asciinema casts with `agg`, then converted to browser-friendly MP4s with
`ffmpeg`.

## Files

- `01-inline-pairing-queue.mp4` shows inline `zeta!` and `zeta?` prompts being
  picked up from the document, queued, and run without blocking the editor.
- `02-region-scoped-edit.mp4` shows a region-scoped edit where Zeta can read
  surrounding context but only edits the selected passage.
- `03-stale-edit-protection.mp4` shows stale edit protection: when the human
  changes the target text while Zeta is running, the exact-match edit is
  refused instead of overwriting the human change.

Each `.cast` file is the source terminal recording; each `.gif` and `.mp4` is a
rendered derivative.

## Regeneration

```sh
agg --theme github-dark --font-size 16 --cols 96 --rows 28 \
  emacs/demos/01-inline-pairing-queue.cast \
  emacs/demos/01-inline-pairing-queue.gif

ffmpeg -y -i emacs/demos/01-inline-pairing-queue.gif \
  -vf 'scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=30' \
  -movflags faststart -pix_fmt yuv420p \
  emacs/demos/01-inline-pairing-queue.mp4
```
