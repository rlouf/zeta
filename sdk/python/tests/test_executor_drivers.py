from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from test_executor_runtime import _open_request
from zeta_executor_daytona import DaytonaExecutor
from zeta_executor_modal import ModalExecutor
from zeta_plugin.executor_runtime import MARKER_PATH, RESULT_PATH


class _Files:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.write_count = 0

    def write_bytes(self, value: bytes, path: str) -> None:
        self.values[path] = value
        self.write_count += 1

    def write_text(self, value: str, path: str) -> None:
        self.write_bytes(value.encode("utf-8"), path)

    def read_text(self, path: str) -> str:
        return self.values[path].decode("utf-8")

    def upload_file(self, value: bytes, path: str) -> None:
        self.write_bytes(value, path)

    def download_file(self, path: str) -> bytes:
        return self.values[path]


class _Process:
    def wait(self) -> int:
        return 0


class _ModalSandbox:
    def __init__(self, name: str | None) -> None:
        self.name = name
        self.object_id = "modal-resource"
        self.filesystem = _Files()
        self.terminated = False
        self.detached = False

    def exec(self, *args: str, **kwargs: Any) -> _Process:
        self.filesystem.write_text(json.dumps({"result": {"provider": "modal"}}), RESULT_PATH)
        return _Process()

    def terminate(self, *, wait: bool) -> None:
        self.terminated = wait

    def detach(self) -> None:
        self.detached = True


class _ModalNotFoundError(Exception):
    pass


class _ModalSandboxApi:
    def __init__(self) -> None:
        self.by_name: dict[str, _ModalSandbox] = {}
        self.created: list[_ModalSandbox] = []

    def from_name(self, app_name: str, name: str) -> _ModalSandbox:
        if name not in self.by_name:
            raise _ModalNotFoundError()
        return self.by_name[name]

    def create(self, *args: str, **options: Any) -> _ModalSandbox:
        sandbox = _ModalSandbox(options.get("name"))
        self.created.append(sandbox)
        if sandbox.name is not None:
            self.by_name[sandbox.name] = sandbox
        return sandbox


class _ModalApp:
    @staticmethod
    def lookup(name: str, *, create_if_missing: bool) -> str:
        assert name == "zeta-test"
        assert create_if_missing
        return name


class _ModalModule:
    def __init__(self) -> None:
        self.Sandbox = _ModalSandboxApi()
        self.App = _ModalApp
        self.exception = type("ExceptionModule", (), {"NotFoundError": _ModalNotFoundError})


class DaytonaNotFoundError(Exception):
    pass


class _DaytonaSandbox:
    def __init__(self, name: str | None) -> None:
        self.name = name
        self.id = "daytona-resource"
        self.fs = _Files()
        self.process = self
        self.deleted = False

    def exec(self, command: str, **kwargs: Any) -> _Process:
        assert command == "python3 /zeta/runtime/remote_runner.py /zeta/runtime/request.json /zeta/runtime/result.json"
        self.fs.upload_file(json.dumps({"result": {"provider": "daytona"}}).encode(), RESULT_PATH)
        return _Process()

    def delete(self, *, wait: bool) -> None:
        self.deleted = wait


class _DaytonaClient:
    def __init__(self) -> None:
        self.by_name: dict[str, _DaytonaSandbox] = {}
        self.created: list[_DaytonaSandbox] = []

    def get(self, name: str) -> _DaytonaSandbox:
        if name not in self.by_name:
            raise DaytonaNotFoundError()
        return self.by_name[name]

    def create(self, params: Mapping[str, Any], *, timeout: int) -> _DaytonaSandbox:
        sandbox = _DaytonaSandbox(params.get("name"))
        self.created.append(sandbox)
        if sandbox.name is not None:
            self.by_name[sandbox.name] = sandbox
        return sandbox


def test_modal_driver_stages_once_and_reconnects_to_a_named_sandbox() -> None:
    module = _ModalModule()
    request = _open_request()
    request["reuse"] = "session"
    request["instance_name"] = "zeta-test"
    request["policy"]["modal"] = {"app": "zeta-test"}
    executor = ModalExecutor(module)

    opened = asyncio.run(executor.open(request, {}))
    result = asyncio.run(
        executor.call({"handle": opened["handle"], "capability": "workspace.read", "input": {"path": "a"}}, {})
    )
    asyncio.run(executor.close({"handle": opened["handle"], "disposition": "release"}, {}))
    sandbox = module.Sandbox.created[0]
    writes = sandbox.filesystem.write_count
    resumed = ModalExecutor(module)
    attached = asyncio.run(resumed.open(request, {}))

    assert result == {"provider": "modal"}
    assert sandbox.filesystem.read_text(MARKER_PATH)
    assert sandbox.filesystem.write_count == writes
    assert attached["resource_id"] == "modal-resource"


def test_daytona_driver_stages_once_and_reconnects_to_a_named_sandbox() -> None:
    client = _DaytonaClient()
    request = _open_request()
    request["reuse"] = "durable"
    request["instance_name"] = "zeta-test"
    executor = DaytonaExecutor(lambda: client, lambda **values: values)

    opened = asyncio.run(executor.open(request, {}))
    result = asyncio.run(
        executor.call({"handle": opened["handle"], "capability": "workspace.read", "input": {"path": "a"}}, {})
    )
    asyncio.run(executor.close({"handle": opened["handle"], "disposition": "release"}, {}))
    sandbox = client.created[0]
    writes = sandbox.fs.write_count
    resumed = DaytonaExecutor(lambda: client, lambda **values: values)
    attached = asyncio.run(resumed.open(request, {}))

    assert result == {"provider": "daytona"}
    assert sandbox.fs.download_file(MARKER_PATH)
    assert sandbox.fs.write_count == writes
    assert attached["resource_id"] == "daytona-resource"
