"""Explicit command execution with bounded stdout/stderr capture."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
import time
from typing import BinaryIO, Protocol

import click

from ..session import record_turn
from ._base import cli

DEFAULT_CAPTURE_BYTES = 6000
READ_SIZE = 65536


class TextStream(Protocol):
    def write(self, text: str) -> object: ...
    def flush(self) -> object: ...


class TailBuffer:
    """Keep the last N bytes written by one command stream."""

    def __init__(self, limit: int) -> None:
        self.limit = max(0, limit)
        self.data = bytearray()

    def append(self, chunk: bytes) -> None:
        if self.limit == 0 or not chunk:
            return
        self.data.extend(chunk)
        overflow = len(self.data) - self.limit
        if overflow > 0:
            del self.data[:overflow]

    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace")


@cli.command(
    "run",
    hidden=True,
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
        "allow_interspersed_args": False,
    },
)
@click.pass_context
@click.option(
    "--shell",
    "use_shell",
    is_flag=True,
    help="Run the remaining argument text through the configured shell.",
)
@click.argument("argv", nargs=-1, type=click.UNPROCESSED)
def cmd_run(ctx: click.Context, use_shell: bool, argv: tuple[str, ...]) -> int:
    """Run a command, stream output live, and record clean output snippets.

    Sigil flags must come before the command; everything after the first
    command word (or after `--`) belongs to the command itself.
    """
    if not argv:
        raise click.UsageError("missing command to run")

    capture_bytes = configured_capture_bytes()
    stdout_tail = TailBuffer(capture_bytes)
    stderr_tail = TailBuffer(capture_bytes)
    command = command_text(argv, use_shell)
    started = time.monotonic()

    try:
        proc = start_process(argv, command, use_shell)
    except FileNotFoundError as error:
        program = error.filename or missing_program(argv, use_shell)
        stderr = (
            f"sigil: missing executable: {program}\n"
            "Install it or make sure it is on PATH, then retry.\n"
        )
        click.echo(stderr, err=True, nl=False)
        record_turn(command, 127, os.getcwd(), stderr_snippet=stderr)
        ctx.exit(127)
    except PermissionError as error:
        target = error.filename or missing_program(argv, use_shell)
        stderr = f"sigil: permission denied: {target}\n"
        click.echo(stderr, err=True, nl=False)
        record_turn(command, 126, os.getcwd(), stderr_snippet=stderr)
        ctx.exit(126)
    assert proc.stdout is not None
    assert proc.stderr is not None

    stdout_thread = threading.Thread(
        target=mirror_stream,
        args=(proc.stdout, sys.stdout, stdout_tail),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=mirror_stream,
        args=(proc.stderr, sys.stderr, stderr_tail),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    try:
        status = proc.wait()
    except KeyboardInterrupt:
        status = proc.wait()
    stdout_thread.join()
    stderr_thread.join()
    proc.stdout.close()
    proc.stderr.close()
    if status < 0:
        status = 128 - status

    record_turn(
        command,
        status,
        os.getcwd(),
        stdout_snippet=stdout_tail.text(),
        stderr_snippet=stderr_tail.text(),
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    ctx.exit(status)


def configured_capture_bytes() -> int:
    raw = os.environ.get("SIGIL_RUN_CAPTURE_BYTES", str(DEFAULT_CAPTURE_BYTES))
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_CAPTURE_BYTES


def command_text(argv: tuple[str, ...], use_shell: bool) -> str:
    """Return the user-facing command text to run and record."""
    if use_shell:
        return " ".join(argv)
    return shlex.join(argv)


def start_process(
    argv: tuple[str, ...],
    command: str,
    use_shell: bool,
) -> subprocess.Popen[bytes]:
    """Start a captured command in argv or shell-string mode."""
    if not use_shell:
        return subprocess.Popen(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=None,
            text=False,
        )

    return subprocess.Popen(
        command,
        shell=True,
        executable=configured_shell_executable() or None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=None,
        text=False,
    )


def configured_shell_executable() -> str:
    """Return the shell executable used for `sigil run --shell`."""
    return os.environ.get("SIGIL_RUN_SHELL") or os.environ.get("SHELL") or ""


def missing_program(argv: tuple[str, ...], use_shell: bool) -> str:
    """Return the executable name to show in process-start failures."""
    if use_shell:
        return configured_shell_executable() or "shell"
    return argv[0]


def mirror_stream(source: BinaryIO, target: TextStream, tail: TailBuffer) -> None:
    while True:
        chunk = source.read(READ_SIZE)
        if not chunk:
            break
        tail.append(chunk)
        write_bytes(target, chunk)


def write_bytes(target: TextStream, chunk: bytes) -> None:
    buffer = getattr(target, "buffer", None)
    if buffer is not None:
        buffer.write(chunk)
        buffer.flush()
        return
    target.write(chunk.decode("utf-8", errors="replace"))
    target.flush()
