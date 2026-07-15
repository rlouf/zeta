"""Import boundary tests for package ownership."""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def test_zeta_source_does_not_import_commas() -> None:
    root = Path(__file__).resolve().parents[1] / "src"
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "commas" or alias.name.startswith("commas."):
                        offenders.append(f"{path}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "commas" or module.startswith("commas."):
                    offenders.append(f"{path}:{node.lineno}")
    assert offenders == []


def test_substrate_source_does_not_import_higher_layers() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "zeta" / "substrate"
    offenders: list[str] = []
    stdlib = sys.stdlib_module_names | {"__future__"}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                modules.append(node.module)
            for module in modules:
                root_module = module.split(".", 1)[0]
                location = getattr(node, "lineno", 0)
                if root_module == "zeta" and not module.startswith("zeta.substrate"):
                    offenders.append(f"{path}:{location}")
                elif root_module not in stdlib and root_module != "zeta":
                    offenders.append(f"{path}:{location}")
    assert offenders == []
