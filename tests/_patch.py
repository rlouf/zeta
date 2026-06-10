from __future__ import annotations

from collections.abc import MutableMapping
from contextlib import contextmanager
from importlib import import_module
from typing import Any

import pytest

_MISSING = object()


def _resolve_target(target: str) -> tuple[object, str]:
    parts = target.split(".")
    for index in range(len(parts) - 1, 0, -1):
        module_name = ".".join(parts[:index])
        try:
            parent = import_module(module_name)
        except ModuleNotFoundError:
            continue
        for attr in parts[index:-1]:
            parent = getattr(parent, attr)
        return parent, parts[-1]
    raise ModuleNotFoundError(target)


@contextmanager
def patch(
    target: str,
    new: Any = _MISSING,
    /,
    *,
    return_value: Any = _MISSING,
    side_effect: Any = _MISSING,
):
    parent, attr = _resolve_target(target)
    with pytest.MonkeyPatch.context() as monkeypatch:
        if new is not _MISSING:
            replacement = new
        elif side_effect is not _MISSING:

            def replacement(*args: object, **kwargs: object) -> Any:
                if isinstance(side_effect, BaseException):
                    raise side_effect
                return side_effect(*args, **kwargs)

        elif return_value is not _MISSING:

            def replacement(*args: object, **kwargs: object) -> Any:
                del args, kwargs
                return return_value

        else:
            raise TypeError("patch requires return_value or side_effect")

        monkeypatch.setattr(parent, attr, replacement)
        yield replacement


@contextmanager
def patch_dict(
    mapping: MutableMapping[str, str],
    values: dict[str, str],
    *,
    clear: bool = False,
):
    with pytest.MonkeyPatch.context() as monkeypatch:
        if clear:
            for key in list(mapping):
                monkeypatch.delitem(mapping, key, raising=False)
        for key, value in values.items():
            monkeypatch.setitem(mapping, key, value)
        yield
