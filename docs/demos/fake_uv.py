#!/usr/bin/env python3
"""Minimal `uv` shim used by generated demo commands inside temp repos."""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    if argv[:2] == ["run", "pytest"] and len(argv) == 2:
        print("tests/test_parser.py::test_parse_value_keeps_numbers_numeric FAILED")
        print(
            "AssertionError: expected parse_value('42') to stay numeric",
            file=sys.stderr,
        )
        print("1 failed")
        return 1
    if len(argv) >= 3 and argv[:2] == ["run", "pytest"]:
        target = " ".join(argv[2:])
        print(f"{target}::test_parse_value_keeps_numbers_numeric PASSED")
        print("1 passed")
        return 0
    if len(argv) >= 3 and argv[:2] == ["run", "ruff"]:
        print("2 files left unchanged")
        return 0
    print("uv demo shim: " + " ".join(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
