"""Hidden recording commands invoked by the shell bindings."""

from __future__ import annotations

import os
import signal

import click

from ._base import cli
from ..failure import record_failure
from ..session import record_turn
from ..tty import open_capture_pty, relay_capture


@cli.command("record-failure", hidden=True)
@click.option("--status", type=int, required=True)
@click.option("--cwd")
@click.option("--stdout-snippet", default="")
@click.option("--stderr-snippet", default="")
@click.argument("command")
def cmd_record_failure(
    command: str,
    status: int,
    cwd: str | None,
    stdout_snippet: str,
    stderr_snippet: str,
) -> int:
    """Record a failed shell command for later comma proposal context."""
    record_failure(command, status, cwd, stdout_snippet, stderr_snippet)
    return 0


@cli.command("record-turn", hidden=True)
@click.option("--status", type=int, required=True)
@click.option("--cwd")
@click.option("--stdout-snippet", default="")
@click.option("--stderr-snippet", default="")
@click.argument("command")
def cmd_record_turn(
    command: str,
    status: int,
    cwd: str | None,
    stdout_snippet: str,
    stderr_snippet: str,
) -> int:
    """Record one shell turn; fans out to failure recording on non-zero exit."""
    record_turn(command, status, cwd, stdout_snippet, stderr_snippet)
    return 0


@cli.command("capture-relay", hidden=True)
@click.option("--sink", required=True)
@click.option("--mirror-fd", type=int, default=None)
def cmd_capture_relay(sink: str, mirror_fd: int | None) -> int:
    """Open a capture pty and fork a relay; print the slave path and reader PID.

    The shell redirects one command stream onto the printed slave path so the
    command keeps a real tty, while the forked reader mirrors the master to
    ``--mirror-fd`` (the command's original stream) and the sink file until it is
    sent SIGTERM.
    """
    master_fd, slave_fd, slave_path = open_capture_pty(mirror_fd)
    pid = os.fork()
    if pid > 0:
        os.close(master_fd)
        os.close(slave_fd)
        print(f"{slave_path} {pid}")
        return 0
    run_capture_reader(master_fd, slave_fd, sink, mirror_fd)
    return 0


def run_capture_reader(
    master_fd: int, slave_fd: int, sink: str, mirror_fd: int | None
) -> None:
    """Run the detached relay child: mirror the pty master until SIGTERM."""
    os.setsid()
    if mirror_fd is not None and mirror_fd <= 2:
        mirror_fd = os.dup(mirror_fd)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    os.close(devnull)

    stop = False

    def request_stop(*_: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    relay_capture(
        master_fd,
        sink,
        mirror_fd=mirror_fd,
        slave_fd=slave_fd,
        should_stop=lambda: stop,
    )
    os.close(master_fd)
    os._exit(0)
