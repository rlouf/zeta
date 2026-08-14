"""Run verified Zeta tool bundles in Daytona Sandboxes."""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from zeta_plugin import ProviderError, executor, providers
from zeta_plugin.executor_runtime import (
    MARKER_PATH,
    REQUEST_PATH,
    RESULT_PATH,
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


@executor("daytona")
class DaytonaExecutor:
    """Execute one Zeta tool bundle in a Daytona Sandbox."""

    def __init__(
        self,
        client_factory: Callable[[], Any] | None = None,
        params_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._params_factory = params_factory
        self._leases: dict[str, _Lease] = {}

    async def open(
        self, request: Mapping[str, Any], context: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Open or attach to a Daytona Sandbox for one verified bundle."""

        try:
            open_request = parse_open_request(request)
            sandbox, created = self._open_sandbox(open_request)
            if created:
                self._stage_bundle(sandbox, open_request)
            else:
                self._verify_marker(sandbox, open_request)
            handle = uuid.uuid4().hex
            self._leases[handle] = _Lease(sandbox, open_request)
            return {"handle": handle, "resource_id": str(getattr(sandbox, "id", ""))}
        except ExecutorRequestError as error:
            raise ProviderError(
                "the executor open request is invalid",
                code="invalid_executor_request",
            ) from error
        except ProviderError:
            raise
        except Exception as error:
            raise ProviderError(
                "the Daytona executor could not open a sandbox",
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
            lease.sandbox.fs.upload_file(
                json.dumps(payload).encode("utf-8"), REQUEST_PATH
            )
            response = lease.sandbox.process.exec(
                "python3 /zeta/runtime/remote_runner.py "
                "/zeta/runtime/request.json /zeta/runtime/result.json",
                cwd=WORKSPACE_ROOT,
                timeout=_call_seconds(lease.request.policy),
            )
            if getattr(response, "exit_code", 0) not in {0, None}:
                return self._read_result(lease.sandbox)
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
                "the Daytona executor call failed",
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
        """Release or terminate one Daytona Sandbox lease."""

        lease = self._lease(request)
        handle = _required_string(request, "handle")
        disposition = _required_string(request, "disposition")
        if disposition not in {"release", "terminate"}:
            raise ProviderError("the close disposition is invalid", code="invalid_executor_request")
        self._leases.pop(handle, None)
        try:
            if disposition == "terminate":
                lease.sandbox.delete(wait=True)
            return {"closed": handle}
        except Exception as error:
            raise ProviderError(
                "the Daytona executor could not close a sandbox",
                code="executor_close_failed",
                retryable=True,
            ) from error

    def _open_sandbox(self, request: OpenRequest) -> tuple[Any, bool]:
        client = self._client()
        name = request.instance_name
        if name is not None:
            try:
                sandbox = client.get(name)
            except Exception as error:
                if error.__class__.__name__ != "DaytonaNotFoundError":
                    raise
            else:
                start = getattr(sandbox, "start", None)
                if callable(start):
                    start()
                return sandbox, False
        return client.create(self._params(request), timeout=_create_seconds(request.policy)), True

    def _client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        try:
            from daytona import Daytona
        except ImportError as error:
            raise ProviderError(
                "install zeta-executor-daytona with its Daytona dependency",
                code="executor_dependency_missing",
            ) from error
        return Daytona()

    def _params(self, request: OpenRequest) -> Any:
        configuration = _driver_config(request.policy, "daytona")
        values: dict[str, Any] = {
            "language": "python",
            "network_block_all": _network_is_blocked(request.policy),
            "ephemeral": request.reuse == "call",
        }
        if request.instance_name is not None:
            values["name"] = request.instance_name
        snapshot = configuration.get("snapshot")
        if snapshot is not None:
            if not isinstance(snapshot, str) or not snapshot:
                raise ExecutorRequestError("the Daytona snapshot is invalid")
            values["snapshot"] = snapshot
        idle_seconds = _idle_seconds(request.policy)
        if idle_seconds is not None:
            values["auto_stop_interval"] = max(1, math.ceil(idle_seconds / 60))
        if self._params_factory is not None:
            return self._params_factory(**values)
        try:
            from daytona import CreateSandboxFromSnapshotParams
        except ImportError as error:
            raise ProviderError(
                "install zeta-executor-daytona with its Daytona dependency",
                code="executor_dependency_missing",
            ) from error
        return CreateSandboxFromSnapshotParams(**values)

    def _stage_bundle(self, sandbox: Any, request: OpenRequest) -> None:
        for file in request.workspace_files:
            self._upload_file(sandbox, WORKSPACE_ROOT, file)
        for file in request.runtime_files():
            self._upload_file(sandbox, RUNTIME_ROOT, file)
        sandbox.fs.upload_file(request.marker.encode("utf-8"), MARKER_PATH)

    def _verify_marker(self, sandbox: Any, request: OpenRequest) -> None:
        marker = sandbox.fs.download_file(MARKER_PATH).decode("utf-8")
        if marker != request.marker:
            raise ProviderError(
                "the named Daytona sandbox has a different Zeta bundle",
                code="executor_bundle_mismatch",
            )

    @staticmethod
    def _upload_file(sandbox: Any, root: str, file: BundleFile) -> None:
        sandbox.fs.upload_file(file.content, f"{root}/{file.path}")

    @staticmethod
    def _read_result(sandbox: Any) -> Mapping[str, Any]:
        value = json.loads(sandbox.fs.download_file(RESULT_PATH).decode("utf-8"))
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


def _create_seconds(policy: Mapping[str, Any]) -> int:
    value = _limits(policy).get("create_seconds", 120)
    if not isinstance(value, int) or value <= 0:
        raise ExecutorRequestError("the create limit is invalid")
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
    raise ExecutorRequestError("the Daytona network policy is invalid")


provider = providers(DaytonaExecutor)
