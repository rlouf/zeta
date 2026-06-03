from __future__ import annotations

import os
from io import StringIO
from typing import cast

from _patch import patch
from sigil.stream import inherited_terminal_fds, run_zeta_stream


def test_inherited_terminal_fds_keeps_valid_zeta_tty_fd() -> None:
    fd = os.open(os.devnull, os.O_RDONLY)
    try:
        assert inherited_terminal_fds({"ZETA_TTY_FD": str(fd)}) == (fd,)
    finally:
        os.close(fd)


def test_inherited_terminal_fds_ignores_missing_zeta_tty_fd() -> None:
    assert inherited_terminal_fds({}) == ()
    assert inherited_terminal_fds({"ZETA_TTY_FD": "not-a-fd"}) == ()
    assert inherited_terminal_fds({"ZETA_TTY_FD": "-1"}) == ()


def test_run_zeta_stream_passes_zeta_tty_fd_to_zeta_process() -> None:
    class FakeProc:
        def __init__(self) -> None:
            self.stdout = StringIO("")

        def wait(self) -> int:
            return 0

    captured: dict[str, object] = {}

    def fake_popen(cmd: list[str], **kwargs: object) -> FakeProc:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return FakeProc()

    fd = os.open(os.devnull, os.O_RDONLY)
    try:
        with patch("sigil.stream.subprocess.Popen", side_effect=fake_popen):
            result = run_zeta_stream(
                ["zeta", "--mode", "json"],
                zeta_env={"ZETA_TTY_FD": str(fd)},
                capture_answer=False,
                capture_trace=False,
            )
    finally:
        os.close(fd)

    assert result == 0
    assert captured["cmd"] == ["zeta", "--mode", "json"]
    kwargs = cast(dict[str, object], captured["kwargs"])
    assert kwargs["pass_fds"] == (fd,)
