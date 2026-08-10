"""Import boundary tests for package ownership.

The tree states the ontology. These tests keep it stated. Each layer may
depend downward only, so a later change cannot quietly reintroduce the
deployment-shaped coupling the packages were renamed to remove.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

# A layer may not import these layers. The lists are deliberately explicit:
# an unlisted layer is allowed, so a new package does not silently pass.
FORBIDDEN_DEPENDENCIES = {
    "journal": {
        "authoring",
        "capabilities",
        "cli",
        "context",
        "harness",
        "loop",
        "models",
        "rpc",
        "tools",
        "trace",
    },
    "authoring": {"cli", "harness", "loop", "rpc"},
    "harness": {"cli", "rpc"},
    "loop": {"authoring", "cli", "harness", "rpc"},
    "context": {"authoring", "cli", "harness", "loop", "rpc"},
    "models": {"authoring", "cli", "harness", "journal", "loop", "rpc"},
    "trace": {
        "authoring",
        "capabilities",
        "cli",
        "context",
        "harness",
        "loop",
        "rpc",
    },
    "tools": {"authoring", "cli", "harness", "loop", "rpc"},
    "wire": {
        "authoring",
        "capabilities",
        "cli",
        "context",
        "harness",
        "journal",
        "loop",
        "models",
        "rpc",
        "tools",
        "trace",
    },
}

# Leaf modules derive values and hold no state. They import only the standard
# library, blake3 (the address hash), and each other, so any layer may use
# them without creating a cycle.
LEAF_MODULES = ("addresses.py", "ids.py", "paths.py")
LEAF_ALLOWED = {"blake3"} | {
    f"zeta.{name.removesuffix('.py')}" for name in LEAF_MODULES
}

# connectors is the third-party extension surface. It sees the event
# vocabulary and the path leaves, so an installed connector can write a
# downloaded file where the project keeps state, and nothing else. None of
# these reach runtime state, so a connector still cannot touch the journal,
# the queue, or a running agent.
CONNECTOR_ALLOWED = {"zeta.effects", "zeta.events", "zeta.paths"}


def imported_modules(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.append((node.module, node.lineno))
    return found


def python_files(*parts: str) -> list[Path]:
    return sorted(SRC.joinpath(*parts).rglob("*.py"))


def test_zeta_source_does_not_import_commas() -> None:
    offenders: list[str] = []
    for path in python_files():
        for module, lineno in imported_modules(path):
            if module == "commas" or module.startswith("commas."):
                offenders.append(f"{path}:{lineno}")
    assert offenders == []


def test_substrate_source_does_not_import_higher_layers() -> None:
    stdlib = sys.stdlib_module_names | {"__future__"}
    offenders: list[str] = []
    for path in python_files("zeta", "substrate"):
        for module, lineno in imported_modules(path):
            if module in LEAF_ALLOWED:
                continue
            root_module = module.split(".", 1)[0]
            if root_module == "zeta" and not module.startswith("zeta.substrate"):
                offenders.append(f"{path}:{lineno} imports {module}")
            elif root_module not in stdlib and root_module != "zeta":
                offenders.append(f"{path}:{lineno} imports {module}")
    assert offenders == []


def test_layers_depend_downward_only() -> None:
    offenders: list[str] = []
    for layer, forbidden in FORBIDDEN_DEPENDENCIES.items():
        for path in python_files("zeta", layer):
            for module, lineno in imported_modules(path):
                parts = module.split(".")
                if len(parts) < 2 or parts[0] != "zeta":
                    continue
                if parts[1] in forbidden:
                    offenders.append(f"{layer}: {path.name}:{lineno} imports {module}")
    assert offenders == []


def test_connectors_see_only_the_event_vocabulary() -> None:
    offenders: list[str] = []
    for path in python_files("connectors"):
        for module, lineno in imported_modules(path):
            if not module.startswith("zeta"):
                continue
            if module not in CONNECTOR_ALLOWED:
                offenders.append(f"{path.name}:{lineno} imports {module}")
    assert offenders == []


def test_leaf_modules_import_only_leaves() -> None:
    """Leaf modules derive values, so every layer may use them."""
    stdlib = sys.stdlib_module_names | {"__future__"}
    offenders: list[str] = []
    for name in LEAF_MODULES:
        for module, lineno in imported_modules(SRC / "zeta" / name):
            if module in LEAF_ALLOWED:
                continue
            if module.split(".", 1)[0] not in stdlib:
                offenders.append(f"{name}:{lineno} imports {module}")
    assert offenders == []
