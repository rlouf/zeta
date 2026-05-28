# Contributing to Sigil

Sigil is alpha software. Bug reports, reproductions, and focused pull requests
are all welcome.

## Development setup

Sigil uses [uv](https://docs.astral.sh/uv/). Install the dev dependencies:

```sh
uv sync --group dev
```

## Checks

Run the same checks CI runs before opening a pull request:

```sh
uv run pre-commit run --all-files
uv run pytest
```

Pre-commit runs ruff (lint + format), `ty` type checking, vulture, and
complexipy. Tests live in `tests/` and use pytest with indicative function
names rather than test classes.

## Working on the agent routes

The `pi`-backed routes (`?`, `??`, `,,`, `,,,`, `@`, `@@`) need the
[pi-mono](https://github.com/earendil-works/pi) coding-agent CLI and a local
OpenAI-compatible model endpoint. The `,` route needs only the endpoint. See
the README's Requirements section for setup, and run `sigil doctor` to confirm
your environment.

## Demos

Deterministic demo GIFs are rendered from VHS tapes in `docs/demos/`, shimming
external dependencies (model server, `pi`, `uv`):

```sh
scripts/render-demo-gifs.sh
```

## Pull requests

- Keep changes focused and explain the motivation.
- Add or update tests for behavior changes.
- Make sure `pre-commit` and `pytest` pass.
