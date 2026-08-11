"""Bundled Zeta connectors as IPC executables."""

from __future__ import annotations

import json
import sys
from typing import Any


def connector_main(
    argv: list[str],
    *,
    manifest: dict[str, Any],
    run: Any,
) -> None:
    """Serve `--describe` or hand off to the connector's run function.

    The manifest is static metadata (spec §13.1): printing it must not
    need credentials, network access, or project state.
    """
    if argv == ["--describe"]:
        json.dump(manifest, sys.stdout, ensure_ascii=False, sort_keys=True)
        sys.stdout.write("\n")
        return
    if argv:
        raise SystemExit(f"usage: {manifest['id']}: no arguments, or --describe")
    run()
