"""Provider declaration data and validation."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
_REGISTRATION_ATTRIBUTE = "__zeta_plugin_registration__"


class DeclarationError(ValueError):
    """A provider declaration is not valid."""


class ProviderKind(StrEnum):
    """The provider categories that Zeta supports."""

    MODEL = "model"
    TOOL = "tool"
    CONNECTOR = "connector"
    EXECUTOR = "executor"


@dataclass(frozen=True)
class ProviderDeclaration:
    """The static metadata that describes one provider."""

    kind: ProviderKind
    identifier: str
    description: str | None = None
    tool_profile: Mapping[str, str] | None = None
    input_schema: Mapping[str, Any] | None = None
    output_schema: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.identifier):
            message = (
                "A provider identifier must use lower-case names separated by dots: "
                f"{self.identifier!r}"
            )
            raise DeclarationError(message)

        if self.kind is not ProviderKind.MODEL and self.tool_profile is not None:
            raise DeclarationError("Only a model can declare a tool profile")

        if self.kind is not ProviderKind.TOOL and self.description is not None:
            raise DeclarationError("Only a tool can declare a description")

        if self.description is not None and not self.description.strip():
            raise DeclarationError("A tool description must not be empty")

        if self.kind is ProviderKind.TOOL and self.input_schema is None:
            raise DeclarationError("A tool requires an input schema")


@dataclass(frozen=True)
class ProviderRegistration:
    """A declaration and its Python implementation."""

    declaration: ProviderDeclaration
    target: Callable[..., Any] | type[Any]

    def __post_init__(self) -> None:
        if self.declaration.kind is not ProviderKind.TOOL:
            return
        description = self.declaration.description
        if description is None:
            description = _first_docstring_paragraph(self.target)
        if description is None:
            raise DeclarationError(
                "A tool requires a description or a non-empty docstring"
            )
        object.__setattr__(
            self,
            "declaration",
            replace(self.declaration, description=description.strip()),
        )


@dataclass(frozen=True)
class ProviderCollection:
    """An explicit collection for a package entry point."""

    registrations: tuple[ProviderRegistration, ...]


def providers(*targets: object) -> ProviderCollection:
    """Create an explicit provider collection from decorated targets."""

    registrations: list[ProviderRegistration] = []
    for target in targets:
        registration = provider_registration(target)
        if registration is None:
            raise DeclarationError("A provider collection requires decorated targets")
        registrations.append(registration)
    return ProviderCollection(registrations=tuple(registrations))


def attach_registration(
    target: Callable[..., Any] | type[Any], declaration: ProviderDeclaration
) -> Callable[..., Any] | type[Any]:
    """Attach a declaration to a target without changing global state."""

    if not callable(target):
        raise DeclarationError("A provider target must be callable")

    if getattr(target, _REGISTRATION_ATTRIBUTE, None) is not None:
        raise DeclarationError("A provider target can have only one declaration")

    registration = ProviderRegistration(declaration=declaration, target=target)
    setattr(target, _REGISTRATION_ATTRIBUTE, registration)
    return target


def provider_registration(target: object) -> ProviderRegistration | None:
    """Get a target registration, if the target has one."""

    registration = getattr(target, _REGISTRATION_ATTRIBUTE, None)
    if registration is None:
        return None
    if not isinstance(registration, ProviderRegistration):
        raise DeclarationError("A provider registration has an invalid value")
    return registration


def _first_docstring_paragraph(target: object) -> str | None:
    docstring = inspect.getdoc(target)
    if docstring is None:
        return None
    first_paragraph = re.split(r"\n\s*\n", docstring, maxsplit=1)[0]
    description = " ".join(first_paragraph.split())
    return description or None
