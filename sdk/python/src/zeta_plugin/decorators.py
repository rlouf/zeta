"""Decorators for Python provider declarations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from .declarations import (
    DeclarationError,
    ProviderDeclaration,
    ProviderKind,
    attach_registration,
)

Target = TypeVar("Target", bound=Callable[..., Any] | type[Any])


def tool(
    identifier: str,
    *,
    description: str | None = None,
    input_schema: Mapping[str, Any] | None = None,
    output_schema: Mapping[str, Any] | None = None,
) -> Callable[[Target], Target]:
    """Declare a function or class as a Zeta tool provider."""

    declaration = ProviderDeclaration(
        kind=ProviderKind.TOOL,
        identifier=identifier,
        description=description,
        input_schema=input_schema,
        output_schema=output_schema,
    )

    def decorate(target: Target) -> Target:
        return attach_registration(target, declaration)  # type: ignore[return-value]

    return decorate


def model(
    identifier: str,
    *,
    tool_profile: Mapping[str, str] | None = None,
) -> Callable[[Target], Target]:
    """Declare a function or class as a Zeta model provider."""

    declaration = ProviderDeclaration(
        kind=ProviderKind.MODEL,
        identifier=identifier,
        tool_profile=tool_profile,
    )

    def decorate(target: Target) -> Target:
        return attach_registration(target, declaration)  # type: ignore[return-value]

    return decorate


def connector(identifier: str) -> Callable[[Target], Target]:
    """Declare a class as a Zeta connector provider."""

    declaration = ProviderDeclaration(
        kind=ProviderKind.CONNECTOR, identifier=identifier
    )

    def decorate(target: Target) -> Target:
        if not isinstance(target, type):
            raise DeclarationError("A connector target must be a class")
        if not any(
            callable(getattr(target, name, None)) for name in ("deliver", "subscribe")
        ):
            raise DeclarationError("A connector class must define deliver or subscribe")
        return attach_registration(target, declaration)  # type: ignore[return-value]

    return decorate


def executor(identifier: str) -> Callable[[Target], Target]:
    """Declare a trusted execution-environment driver.

    The open request has workspace and tool bundles. It also has the reuse
    mode. Reused environments receive a stable instance name. The close
    request has a disposition.
    """

    declaration = ProviderDeclaration(
        kind=ProviderKind.EXECUTOR, identifier=identifier
    )

    def decorate(target: Target) -> Target:
        if not isinstance(target, type):
            raise DeclarationError("An executor target must be a class")
        required = ("open", "call", "close")
        if any(not callable(getattr(target, name, None)) for name in required):
            raise DeclarationError("An executor class must define open, call, and close")
        return attach_registration(target, declaration)  # type: ignore[return-value]

    return decorate
