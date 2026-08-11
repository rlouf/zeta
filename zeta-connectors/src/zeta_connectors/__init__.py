"""Bundled Zeta connector entry points."""

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
    """Keep manifest discovery free of credentials, network, and project state."""
    if argv == ["--describe"]:
        json.dump(manifest, sys.stdout, ensure_ascii=False, sort_keys=True)
        sys.stdout.write("\n")
        return
    if argv:
        raise SystemExit(f"usage: {manifest['id']}: no arguments, or --describe")
    run()
