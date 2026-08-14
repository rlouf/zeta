"""Private provider host process for the Rust Zeta runtime."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TextIO

from .declarations import ProviderKind
from .discovery import (
    LoadedProvider,
    ProviderCatalog,
    discover_entry_points,
    discover_project,
    resolve_catalog,
)
from .errors import ProviderError
from .protocol import (
    PROTOCOL_VERSION,
    ProtocolError,
    error,
    notification,
    read_message,
    request,
    success,
    write_message,
)

_METHODS = ("providers.catalog", "generate", "invoke", "deliver", "subscribe")
_INVALID_PARAMS = -32602
_METHOD_NOT_FOUND = -32601
_SERVER_ERROR = -32000


@dataclass(frozen=True)
class HostError(Exception):
    """One provider host failure with retry metadata."""

    message: str
    code: int = _SERVER_ERROR
    stable_code: str = "provider_error"
    retryable: bool = False


class ProviderHost:
    """Loads provider declarations and routes private host operations."""

    def __init__(
        self,
        project_root: Path,
        *,
        entry_points: Iterable[Any] | None = None,
    ) -> None:
        local = discover_project(project_root)
        packages = discover_entry_points(entry_points)
        self._catalog = resolve_catalog(local, packages)
        self._instances: dict[tuple[ProviderKind, str], Any] = {}

    @property
    def catalog(self) -> ProviderCatalog:
        """Get the resolved provider catalog."""

        return self._catalog

    def catalog_result(self) -> dict[str, Any]:
        """Return a JSON-safe catalog for the Rust host."""

        return {
            "models": self._descriptors(ProviderKind.MODEL),
            "tools": self._descriptors(ProviderKind.TOOL),
            "connectors": self._descriptors(ProviderKind.CONNECTOR),
        }

    def call(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        observe: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Run one private host operation."""

        if method == "providers.catalog":
            self._input(params)
            return self.catalog_result()
        operation = {
            "generate": (ProviderKind.MODEL, "model", "generate"),
            "invoke": (ProviderKind.TOOL, "tool", "invoke"),
            "deliver": (ProviderKind.CONNECTOR, "connector", "deliver"),
            "subscribe": (ProviderKind.CONNECTOR, "connector", "subscribe"),
        }.get(method)
        if operation is None:
            raise HostError(
                f"Unknown provider host method {method!r}",
                code=_METHOD_NOT_FOUND,
                stable_code="method_not_found",
            )
        kind, provider_field, target_method = operation
        input_value = self._input(params)
        identifier = input_value.get(provider_field)
        if not isinstance(identifier, str) or not identifier:
            raise HostError(
                f"The input field {provider_field!r} must be a non-empty string",
                code=_INVALID_PARAMS,
                stable_code="invalid_provider_identifier",
            )
        request_value = input_value.get("request", {})
        if not isinstance(request_value, Mapping):
            raise HostError(
                "The input field 'request' must be an object",
                code=_INVALID_PARAMS,
                stable_code="invalid_provider_request",
            )
        provider = self._catalog.providers(kind).get(identifier)
        if provider is None:
            raise HostError(
                f"Unknown {kind.value} provider {identifier!r}",
                code=_INVALID_PARAMS,
                stable_code="provider_not_found",
            )
        context = self._context(input_value, params)
        if observe is not None:
            context["observe"] = observe
        handler = self._handler(provider, target_method)
        try:
            result = handler(dict(request_value), context)
            if inspect.isawaitable(result):
                result = asyncio.run(result)
        except HostError:
            raise
        except ProviderError as failure:
            raise HostError(
                failure.message,
                stable_code=failure.code,
                retryable=failure.retryable,
            ) from failure
        except Exception as error:
            raise HostError(
                f"Provider {identifier!r} failed: {error}",
                stable_code="provider_failed",
            ) from error
        if not isinstance(result, Mapping):
            raise HostError(
                f"Provider {identifier!r} returned a non-object result",
                stable_code="invalid_provider_result",
            )
        return dict(result)

    def _descriptors(self, kind: ProviderKind) -> list[dict[str, Any]]:
        descriptors = []
        for identifier, provider in sorted(self._catalog.providers(kind).items()):
            declaration = provider.registration.declaration
            descriptors.append(
                {
                    "id": identifier,
                    "source": {
                        "module": provider.source.module,
                        "path": str(provider.source.path)
                        if provider.source.path is not None
                        else None,
                        "distribution": provider.source.distribution,
                    },
                    "fingerprint": _fingerprint(provider),
                    "description": declaration.description,
                    "tool_profile": dict(declaration.tool_profile)
                    if declaration.tool_profile is not None
                    else None,
                    "input_schema": dict(declaration.input_schema)
                    if declaration.input_schema is not None
                    else None,
                    "output_schema": dict(declaration.output_schema)
                    if declaration.output_schema is not None
                    else None,
                }
            )
        return descriptors

    def _input(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        input_value = params.get("input")
        if not isinstance(input_value, Mapping):
            raise HostError(
                "The parameter 'input' must be an object",
                code=_INVALID_PARAMS,
                stable_code="invalid_input",
            )
        return input_value

    def _context(
        self, input_value: Mapping[str, Any], params: Mapping[str, Any]
    ) -> dict[str, Any]:
        context = input_value.get("context", {})
        if not isinstance(context, Mapping):
            raise HostError(
                "The input field 'context' must be an object",
                code=_INVALID_PARAMS,
                stable_code="invalid_context",
            )
        resolved = dict(context)
        for name in ("base_dir", "effect_key"):
            value = params.get(name)
            if value is not None:
                resolved[name] = value
        return resolved

    def _handler(self, provider: LoadedProvider, operation: str) -> Any:
        target = provider.registration.target
        if isinstance(target, type):
            key = (provider.registration.declaration.kind, provider.identifier)
            target = self._instances.setdefault(key, target())
        if callable(target) and not isinstance(target, type):
            if operation in {"generate", "invoke"}:
                return target
        handler = getattr(target, operation, None)
        if not callable(handler):
            raise HostError(
                f"Provider {provider.identifier!r} does not define {operation!r}",
                code=_INVALID_PARAMS,
                stable_code="unsupported_operation",
            )
        return handler


def serve(
    host: ProviderHost,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
) -> None:
    """Serve the private host protocol until a shutdown request arrives."""

    write_message(
        output_stream,
        request(
            "provider-initialize",
            "initialize",
            {
                "protocol_versions": [PROTOCOL_VERSION],
                "peer": {"name": "zeta-python-host", "version": "0.1.0"},
                "roles": ["provider"],
                "methods": [{"name": name} for name in _METHODS],
                "heartbeat_seconds": 10,
                "max_in_flight": 64,
            },
        ),
    )
    initialized = _read_or_fail(input_stream)
    if initialized.get("id") != "provider-initialize" or "result" not in initialized:
        raise ProtocolError("The runtime did not accept provider initialization")

    while True:
        message = read_message(input_stream)
        if message is None:
            return
        if "method" not in message or "id" not in message:
            continue
        identifier = message["id"]
        if not isinstance(identifier, (str, int)):
            write_message(
                output_stream,
                error(
                    None, _INVALID_PARAMS, "A request id must be a string or integer"
                ),
            )
            continue
        method = message["method"]
        if not isinstance(method, str):
            write_message(
                output_stream,
                error(identifier, _INVALID_PARAMS, "A request method must be a string"),
            )
            continue
        params = message.get("params", {})
        if not isinstance(params, Mapping):
            write_message(
                output_stream,
                error(
                    identifier, _INVALID_PARAMS, "Request parameters must be an object"
                ),
            )
            continue
        if method == "shutdown":
            write_message(output_stream, success(identifier, {}))
            return
        try:
            def observe(value: Mapping[str, Any]) -> None:
                write_message(
                    output_stream,
                    notification(
                        "model.observation",
                        {"observation": _observation(value)},
                    ),
                )

            result = host.call(
                method,
                params,
                observe=observe if method == "generate" else None,
            )
            write_message(output_stream, success(identifier, result))
        except HostError as failure:
            write_message(
                output_stream,
                error(
                    identifier,
                    failure.code,
                    failure.message,
                    stable_code=failure.stable_code,
                    retryable=failure.retryable,
                ),
            )


def main(arguments: list[str] | None = None) -> int:
    """Run the provider host as a supervised child process."""

    parser = argparse.ArgumentParser(description="Run the Zeta Python provider host")
    parser.add_argument("--project-root", type=Path, required=True)
    parsed = parser.parse_args(arguments)
    try:
        serve(ProviderHost(parsed.project_root))
    except (HostError, ProtocolError, OSError) as error:
        print(f"zeta provider host failed: {error}", file=sys.stderr)
        return 1
    return 0


def _read_or_fail(input_stream: TextIO) -> Mapping[str, Any]:
    message = read_message(input_stream)
    if message is None:
        raise ProtocolError("The runtime closed input during provider initialization")
    return message


def _observation(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise HostError(
            "A model observation must be an object",
            code=_INVALID_PARAMS,
            stable_code="invalid_provider_observation",
        )
    kind = value.get("kind")
    if kind in {"text_delta", "reasoning_delta"}:
        text = value.get("text")
        if isinstance(text, str):
            return {"kind": kind, "text": text}
    elif kind == "status":
        status = value.get("status")
        text = value.get("text")
        if isinstance(status, str) and isinstance(text, str):
            return {"kind": kind, "status": status, "text": text}
    raise HostError(
        "The model observation has an invalid shape",
        code=_INVALID_PARAMS,
        stable_code="invalid_provider_observation",
    )


def _fingerprint(provider: LoadedProvider) -> str:
    declaration = provider.registration.declaration
    target = provider.registration.target
    source_file = provider.source.path
    if source_file is None:
        candidate = inspect.getsourcefile(target)
        source_file = Path(candidate) if candidate is not None else None
    digest = hashlib.sha256()
    digest.update(declaration.kind.value.encode())
    digest.update(declaration.identifier.encode())
    digest.update(getattr(target, "__qualname__", type(target).__qualname__).encode())
    digest.update(
        json.dumps(
            {
                "description": declaration.description,
                "tool_profile": declaration.tool_profile,
                "input_schema": declaration.input_schema,
                "output_schema": declaration.output_schema,
            },
            default=dict,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    if source_file is not None:
        try:
            digest.update(source_file.read_bytes())
        except OSError:
            pass
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
