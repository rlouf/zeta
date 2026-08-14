"""Verify executor bundles and provide the portable remote tool runtime."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from blake3 import blake3

WORKSPACE_ROOT = "/zeta/workspace"
RUNTIME_ROOT = "/zeta/runtime"
MARKER_PATH = "/zeta/runtime/bundle.json"
REQUEST_PATH = "/zeta/runtime/request.json"
RESULT_PATH = "/zeta/runtime/result.json"
RUNNER_PATH = "/zeta/runtime/remote_runner.py"


class ExecutorRequestError(ValueError):
    """An executor request does not match the trusted bundle contract."""


@dataclass(frozen=True)
class BundleFile:
    """One verified file that a driver can stage in a remote environment."""

    path: str
    content_address: str
    content: bytes


@dataclass(frozen=True)
class Capability:
    """One tool definition that the portable runtime can dispatch."""

    identifier: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any] | None
    source_path: str


@dataclass(frozen=True)
class OpenRequest:
    """The verified data needed to open one remote executor environment."""

    profile: str
    policy: Mapping[str, Any]
    reuse: str
    instance_name: str | None
    capabilities: tuple[Capability, ...]
    workspace_id: str
    tool_id: str
    workspace_files: tuple[BundleFile, ...]

    @property
    def marker(self) -> str:
        """Return the remote marker for this exact bundle."""

        return canonical_json(
            {
                "tool_bundle": self.tool_id,
                "workspace_bundle": self.workspace_id,
            }
        ).decode("utf-8")

    def has_capability(self, identifier: str) -> bool:
        """Report whether the bundle permits a capability."""

        return any(capability.identifier == identifier for capability in self.capabilities)

    def runtime_files(self) -> tuple[BundleFile, ...]:
        """Return the versioned runtime files for one remote environment."""

        return (
            bundle_file("remote_runner.py", _REMOTE_RUNNER.encode("utf-8")),
            bundle_file("zeta_plugin/__init__.py", _REMOTE_PLUGIN.encode("utf-8")),
        )


def parse_open_request(request: Mapping[str, Any]) -> OpenRequest:
    """Parse and verify an executor open request."""

    profile = required_string(request, "profile")
    policy = required_object(request, "policy")
    reuse = required_string(request, "reuse")
    if reuse not in {"call", "session", "durable"}:
        raise ExecutorRequestError("the executor reuse mode is invalid")
    instance_name = request.get("instance_name")
    if instance_name is not None and (not isinstance(instance_name, str) or not instance_name):
        raise ExecutorRequestError("the executor instance name is invalid")
    if reuse != "call" and instance_name is None:
        raise ExecutorRequestError("a reused executor needs an instance name")

    workspace = required_object(request, "workspace_bundle")
    tools = required_object(request, "tool_bundle")
    workspace_files = parse_files(workspace, "workspace")
    if workspace.get("id") != bundle_identity(
        "workspace", {"files": [file_value(file) for file in workspace_files]}
    ):
        raise ExecutorRequestError("the workspace bundle identity is invalid")
    tool_files = parse_files(tools, "tool")
    capabilities = parse_capabilities(tools)
    if tools.get("id") != bundle_identity(
        "tools",
        {
            "files": [file_value(file) for file in tool_files],
            "capabilities": [capability_value(capability) for capability in capabilities],
        },
    ):
        raise ExecutorRequestError("the tool bundle identity is invalid")
    workspace_by_path = {file.path: file for file in workspace_files}
    for file in tool_files:
        if workspace_by_path.get(file.path) != file:
            raise ExecutorRequestError("a tool file differs from the workspace bundle")
    tool_paths = {file.path for file in tool_files}
    for capability in capabilities:
        if capability.source_path not in tool_paths:
            raise ExecutorRequestError("a capability source is absent from the tool bundle")

    allow_list = request.get("capabilities")
    if not isinstance(allow_list, list) or any(
        not isinstance(identifier, str) for identifier in allow_list
    ):
        raise ExecutorRequestError("the capability allow-list is invalid")
    expected_ids = [capability.identifier for capability in capabilities]
    if allow_list != expected_ids:
        raise ExecutorRequestError("the capability allow-list differs from the tool bundle")

    return OpenRequest(
        profile=profile,
        policy=policy,
        reuse=reuse,
        instance_name=instance_name,
        capabilities=tuple(capabilities),
        workspace_id=required_string(workspace, "id"),
        tool_id=required_string(tools, "id"),
        workspace_files=tuple(workspace_files),
    )


def parse_call_request(
    request: Mapping[str, Any], open_request: OpenRequest
) -> tuple[str, Mapping[str, Any]]:
    """Parse a call and reject capabilities outside the open bundle."""

    identifier = required_string(request, "capability")
    if not open_request.has_capability(identifier):
        raise ExecutorRequestError("the capability is absent from the tool bundle")
    input_value = required_object(request, "input")
    return identifier, input_value


def bundle_file(path: str, content: bytes) -> BundleFile:
    """Create one content-addressed runtime file."""

    return BundleFile(path, content_address(content), content)


def file_value(file: BundleFile) -> dict[str, str]:
    """Return the IPC JSON form for one bundle file."""

    return {
        "path": file.path,
        "content_address": file.content_address,
        "content_base64": base64.b64encode(file.content).decode("ascii"),
    }


def capability_value(capability: Capability) -> dict[str, Any]:
    """Return the IPC JSON form for one capability."""

    return {
        "id": capability.identifier,
        "description": capability.description,
        "input_schema": dict(capability.input_schema),
        "output_schema": (
            dict(capability.output_schema)
            if capability.output_schema is not None
            else None
        ),
        "source_path": capability.source_path,
    }


def content_address(content: bytes) -> str:
    """Return the Zeta BLAKE3 content address for bytes."""

    return f"b3:{blake3(content).hexdigest()}"


def canonical_json(value: Any) -> bytes:
    """Encode the JSON form used for bundle identities."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ExecutorRequestError("the executor request has non-canonical JSON") from error


def required_string(value: Mapping[str, Any], name: str) -> str:
    """Return one required non-empty string field."""

    field = value.get(name)
    if not isinstance(field, str) or not field:
        raise ExecutorRequestError(f"the field {name!r} must be a non-empty string")
    return field


def required_object(value: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    """Return one required object field."""

    field = value.get(name)
    if not isinstance(field, Mapping):
        raise ExecutorRequestError(f"the field {name!r} must be an object")
    return field


def parse_files(value: Mapping[str, Any], kind: str) -> list[BundleFile]:
    """Parse and verify one file list."""

    entries = value.get("files")
    if not isinstance(entries, list):
        raise ExecutorRequestError(f"the {kind} bundle files must be an array")
    files: list[BundleFile] = []
    previous = ""
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ExecutorRequestError(f"a {kind} bundle file is invalid")
        path = required_string(entry, "path")
        validate_path(path)
        if files and path <= previous:
            raise ExecutorRequestError(f"the {kind} bundle file order is invalid")
        previous = path
        encoded = required_string(entry, "content_base64")
        try:
            content = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise ExecutorRequestError(f"a {kind} bundle file has invalid base64") from error
        if entry.get("content_address") != content_address(content):
            raise ExecutorRequestError(f"a {kind} bundle file has an invalid address")
        files.append(BundleFile(path, entry["content_address"], content))
    return files


def parse_capabilities(value: Mapping[str, Any]) -> list[Capability]:
    """Parse one exact capability declaration list."""

    entries = value.get("capabilities")
    if not isinstance(entries, list):
        raise ExecutorRequestError("the tool bundle capabilities must be an array")
    capabilities: list[Capability] = []
    previous = ""
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ExecutorRequestError("a tool bundle capability is invalid")
        identifier = required_string(entry, "id")
        if capabilities and identifier <= previous:
            raise ExecutorRequestError("the tool bundle capability order is invalid")
        previous = identifier
        description = required_string(entry, "description")
        input_schema = required_object(entry, "input_schema")
        output_schema = entry.get("output_schema")
        if output_schema is not None and not isinstance(output_schema, Mapping):
            raise ExecutorRequestError("a tool output schema must be an object")
        source_path = required_string(entry, "source_path")
        validate_path(source_path)
        capabilities.append(
            Capability(
                identifier,
                description,
                input_schema,
                output_schema,
                source_path,
            )
        )
    return capabilities


def bundle_identity(kind: str, value: Mapping[str, Any]) -> str:
    """Return one content-addressed bundle identifier."""

    return f"{kind}:{content_address(canonical_json(value))}"


def validate_path(path: str) -> None:
    """Reject a path that could escape the fixed remote root."""

    value = PurePosixPath(path)
    if (
        value.is_absolute()
        or path.startswith("/")
        or path.endswith("/")
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise ExecutorRequestError("a bundle path is invalid")


def bundle_marker_digest(request: OpenRequest) -> str:
    """Return a safe marker digest for provider diagnostics."""

    return hashlib.sha256(request.marker.encode("utf-8")).hexdigest()


_REMOTE_PLUGIN = '''"""Minimal Zeta decorator support for portable executor tools."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


def tool(
    identifier: str,
    *,
    description: str | None = None,
    input_schema: Mapping[str, Any] | None = None,
    output_schema: Mapping[str, Any] | None = None,
) -> Callable[[Any], Any]:
    def decorate(target: Any) -> Any:
        setattr(target, "__zeta_remote_tool__", {"id": identifier})
        return target

    return decorate
'''


_REMOTE_RUNNER = '''"""Dispatch one verified portable Zeta tool call."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def main(request_path: str, result_path: str) -> int:
    try:
        request = json.loads(Path(request_path).read_text(encoding="utf-8"))
        result = dispatch(request)
        Path(result_path).write_text(
            json.dumps({"result": result}, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    except Exception as error:
        Path(result_path).write_text(
            json.dumps(
                {"error": {"code": "remote_tool_failed", "message": str(error)}},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        return 1
    return 0


def dispatch(request: Mapping[str, Any]) -> Mapping[str, Any]:
    identifier = request.get("capability")
    input_value = request.get("input")
    workspace = request.get("workspace")
    capabilities = request.get("capabilities")
    if not isinstance(identifier, str) or not isinstance(input_value, Mapping):
        raise ValueError("the remote call is invalid")
    if not isinstance(workspace, str) or not isinstance(capabilities, list):
        raise ValueError("the remote call is invalid")
    declaration = next(
        (
            item
            for item in capabilities
            if isinstance(item, Mapping) and item.get("id") == identifier
        ),
        None,
    )
    if declaration is None:
        raise ValueError("the capability is not allowed")
    validate(input_value, declaration.get("input_schema"), "input")
    source_path = declaration.get("source_path")
    if not isinstance(source_path, str) or source_path.startswith("/"):
        raise ValueError("the capability source path is invalid")
    root = Path(workspace).resolve()
    source = (root / source_path).resolve()
    if root not in source.parents or not source.is_file():
        raise ValueError("the capability source is invalid")
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location("zeta_remote_tool", source)
    if spec is None or spec.loader is None:
        raise ValueError("the capability source cannot load")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = next(
        (
            candidate
            for candidate in vars(module).values()
            if callable(candidate)
            and getattr(candidate, "__zeta_remote_tool__", {}).get("id") == identifier
        ),
        None,
    )
    if target is None:
        raise ValueError("the capability implementation is unavailable")
    context = {"base_dir": str(root)}
    effect_key = request.get("effect_key")
    if isinstance(effect_key, str):
        context["effect_key"] = effect_key
    result = target(dict(input_value), context)
    if inspect.isawaitable(result):
        result = asyncio.run(result)
    if not isinstance(result, Mapping):
        raise ValueError("the capability result is not an object")
    schema = declaration.get("output_schema")
    if schema is not None:
        validate(result, schema, "result")
    return dict(result)


def validate(value: Any, schema: Any, location: str) -> None:
    if not isinstance(schema, Mapping):
        raise ValueError(f"the {location} schema is invalid")
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, Mapping):
            raise ValueError(f"the {location} must be an object")
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(key, str) for key in required):
            raise ValueError(f"the {location} schema is invalid")
        if any(key not in value for key in required):
            raise ValueError(f"the {location} has a missing required field")
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            raise ValueError(f"the {location} schema is invalid")
        if schema.get("additionalProperties") is False and any(
            key not in properties for key in value
        ):
            raise ValueError(f"the {location} has an unknown field")
        for key, child in properties.items():
            if key in value:
                validate(value[key], child, f"{location}.{key}")
    elif expected == "array":
        if not isinstance(value, list):
            raise ValueError(f"the {location} must be an array")
        if "items" in schema:
            for index, item in enumerate(value):
                validate(item, schema["items"], f"{location}[{index}]")
    elif expected == "string" and not isinstance(value, str):
        raise ValueError(f"the {location} must be a string")
    elif expected == "boolean" and not isinstance(value, bool):
        raise ValueError(f"the {location} must be a boolean")
    elif expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        raise ValueError(f"the {location} must be an integer")
    elif expected == "number" and (
        not isinstance(value, (int, float)) or isinstance(value, bool)
    ):
        raise ValueError(f"the {location} must be a number")
    elif expected == "null" and value is not None:
        raise ValueError(f"the {location} must be null")
    elif expected not in {
        None,
        "null",
        "object",
        "array",
        "string",
        "boolean",
        "integer",
        "number",
    }:
        raise ValueError(f"the {location} schema type is unsupported")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"the {location} is not an allowed value")


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
'''
