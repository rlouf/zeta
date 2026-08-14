"""Run verified Zeta tool bundles in Modal Sandboxes."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from zeta_plugin import ProviderError, executor, providers
from zeta_plugin.executor_runtime import (
    MARKER_PATH,
    REQUEST_PATH,
    RESULT_PATH,
    RUNNER_PATH,
    RUNTIME_ROOT,
    WORKSPACE_ROOT,
    BundleFile,
    ExecutorRequestError,
    OpenRequest,
    capability_value,
    parse_call_request,
    parse_open_request,
)


@dataclass
class _Lease:
    sandbox: Any
    request: OpenRequest


@executor("modal")
class ModalExecutor:
    """Execute one Zeta tool bundle in a Modal Sandbox."""

    def __init__(self, modal_module: Any | None = None) -> None:
        self._modal_module = modal_module
        self._leases: dict[str, _Lease] = {}

    async def open(
        self, request: Mapping[str, Any], context: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Open or attach to a Modal Sandbox for one verified bundle."""

        try:
            open_request = parse_open_request(request)
            sandbox, created = self._open_sandbox(open_request)
            if created:
                self._stage_bundle(sandbox, open_request)
            else:
                self._verify_marker(sandbox, open_request)
            handle = uuid.uuid4().hex
            self._leases[handle] = _Lease(sandbox, open_request)
            return {
                "handle": handle,
                "resource_id": str(getattr(sandbox, "object_id", "")),
            }
        except ExecutorRequestError as error:
            raise ProviderError(
                "the executor open request is invalid",
                code="invalid_executor_request",
            ) from error
        except ProviderError:
            raise
        except Exception as error:
            raise ProviderError(
                "the Modal executor could not open a sandbox",
                code="executor_open_failed",
                retryable=True,
            ) from error

    async def call(
        self, request: Mapping[str, Any], context: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Run one capability through the portable tool runtime."""

        try:
            lease = self._lease(request)
            identifier, input_value = parse_call_request(request, lease.request)
            payload: dict[str, Any] = {
                "capability": identifier,
                "input": dict(input_value),
                "workspace": WORKSPACE_ROOT,
                "capabilities": [
                    capability_value(capability)
                    for capability in lease.request.capabilities
                ],
            }
            effect_key = request.get("effect_key")
            if isinstance(effect_key, str):
                payload["effect_key"] = effect_key
            self._write_text(lease.sandbox, REQUEST_PATH, json.dumps(payload))
            timeout = _call_seconds(lease.request.policy)
            process = lease.sandbox.exec(
                "python3",
                RUNNER_PATH,
                REQUEST_PATH,
                RESULT_PATH,
                timeout=timeout,
                workdir=WORKSPACE_ROOT,
            )
            process.wait()
            return self._read_result(lease.sandbox)
        except ExecutorRequestError as error:
            raise ProviderError(
                "the executor call request is invalid",
                code="invalid_executor_request",
            ) from error
        except ProviderError:
            raise
        except Exception as error:
            raise ProviderError(
                "the Modal executor call failed",
                code="executor_call_failed",
                retryable=True,
            ) from error

    async def cancel(
        self, request: Mapping[str, Any], context: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Report cancellation support for the current one-shot runtime."""

        self._lease(request)
        return {"cancelled": False}

    async def close(
        self, request: Mapping[str, Any], context: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Release or terminate one Modal Sandbox lease."""

        lease = self._lease(request)
        handle = _required_string(request, "handle")
        disposition = _required_string(request, "disposition")
        if disposition not in {"release", "terminate"}:
            raise ProviderError("the close disposition is invalid", code="invalid_executor_request")
        self._leases.pop(handle, None)
        try:
            if disposition == "terminate":
                lease.sandbox.terminate(wait=True)
            detach = getattr(lease.sandbox, "detach", None)
            if callable(detach):
                detach()
            return {"closed": handle}
        except Exception as error:
            raise ProviderError(
                "the Modal executor could not close a sandbox",
                code="executor_close_failed",
                retryable=True,
            ) from error

    def _modal(self) -> Any:
        if self._modal_module is not None:
            return self._modal_module
        try:
            import modal
        except ImportError as error:
            raise ProviderError(
                "install zeta-executor-modal with its Modal dependency",
                code="executor_dependency_missing",
            ) from error
        return modal

    def _open_sandbox(self, request: OpenRequest) -> tuple[Any, bool]:
        modal = self._modal()
        configuration = _driver_config(request.policy, "modal")
        app_name = _required_string(configuration, "app")
        name = request.instance_name
        if name is not None:
            try:
                return modal.Sandbox.from_name(app_name, name), False
            except modal.exception.NotFoundError:
                pass
        app = modal.App.lookup(app_name, create_if_missing=True)
        options: dict[str, Any] = {
            "app": app,
            "block_network": _network_is_blocked(request.policy),
            "timeout": _sandbox_seconds(request.policy),
        }
        idle_seconds = _idle_seconds(request.policy)
        if idle_seconds is not None:
            options["idle_timeout"] = idle_seconds
        image_name = configuration.get("image")
        if image_name is not None:
            if not isinstance(image_name, str) or not image_name:
                raise ExecutorRequestError("the Modal image is invalid")
            options["image"] = modal.Image.from_name(image_name)
        if name is not None:
            options["name"] = name
        return modal.Sandbox.create("sleep", "infinity", **options), True

    def _stage_bundle(self, sandbox: Any, request: OpenRequest) -> None:
        for file in request.workspace_files:
            self._write_file(sandbox, WORKSPACE_ROOT, file)
        for file in request.runtime_files():
            self._write_file(sandbox, RUNTIME_ROOT, file)
        self._write_text(sandbox, MARKER_PATH, request.marker)

    def _verify_marker(self, sandbox: Any, request: OpenRequest) -> None:
        marker = sandbox.filesystem.read_text(MARKER_PATH)
        if marker != request.marker:
            raise ProviderError(
                "the named Modal sandbox has a different Zeta bundle",
                code="executor_bundle_mismatch",
            )

    @staticmethod
    def _write_file(sandbox: Any, root: str, file: BundleFile) -> None:
        sandbox.filesystem.write_bytes(file.content, f"{root}/{file.path}")

    @staticmethod
    def _write_text(sandbox: Any, path: str, value: str) -> None:
        sandbox.filesystem.write_text(value, path)

    @staticmethod
    def _read_result(sandbox: Any) -> Mapping[str, Any]:
        value = json.loads(sandbox.filesystem.read_text(RESULT_PATH))
        if not isinstance(value, Mapping):
            raise ProviderError("the remote tool returned invalid JSON", code="executor_result_invalid")
        error = value.get("error")
        if isinstance(error, Mapping):
            raise ProviderError("the remote tool failed", code="remote_tool_failed")
        result = value.get("result")
        if not isinstance(result, Mapping):
            raise ProviderError("the remote tool returned no object", code="executor_result_invalid")
        return dict(result)

    def _lease(self, request: Mapping[str, Any]) -> _Lease:
        handle = _required_string(request, "handle")
        lease = self._leases.get(handle)
        if lease is None:
            raise ProviderError("the executor handle is unknown", code="executor_handle_unknown")
        return lease


def _driver_config(policy: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = policy.get(name, {})
    if not isinstance(value, Mapping):
        raise ExecutorRequestError(f"the {name} executor configuration is invalid")
    return value


def _required_string(value: Mapping[str, Any], name: str) -> str:
    field = value.get(name)
    if not isinstance(field, str) or not field:
        raise ExecutorRequestError(f"the field {name!r} is invalid")
    return field


def _limits(policy: Mapping[str, Any]) -> Mapping[str, Any]:
    value = policy.get("limits", {})
    if not isinstance(value, Mapping):
        raise ExecutorRequestError("the executor limits are invalid")
    return value


def _call_seconds(policy: Mapping[str, Any]) -> int:
    value = _limits(policy).get("call_seconds", 300)
    if not isinstance(value, int) or value <= 0:
        raise ExecutorRequestError("the call limit is invalid")
    return value


def _sandbox_seconds(policy: Mapping[str, Any]) -> int:
    value = _limits(policy).get("sandbox_seconds", 86_400)
    if not isinstance(value, int) or value <= 0:
        raise ExecutorRequestError("the sandbox limit is invalid")
    return value


def _idle_seconds(policy: Mapping[str, Any]) -> int | None:
    value = _limits(policy).get("idle_seconds")
    if value is None:
        return None
    if not isinstance(value, int) or value < 0:
        raise ExecutorRequestError("the idle limit is invalid")
    return value


def _network_is_blocked(policy: Mapping[str, Any]) -> bool:
    network = policy.get("network", "none")
    if network == "none":
        return True
    if network == "full":
        return False
    raise ExecutorRequestError("the Modal network policy is invalid")


provider = providers(ModalExecutor)
