"""Terminal confirmation helpers shared by CLI and operator runtimes."""

from __future__ import annotations

import fcntl
import os
import pty
import select
import sys
import termios
import time
from typing import BinaryIO, Callable


def confirm_on_tty(prompt: str) -> bool:
    """Read a yes/no confirmation from the controlling terminal."""
    answer = prompt_on_tty(prompt)
    return bool(answer and answer.strip().lower() in {"y", "yes"})


def prompt_on_tty(prompt: str) -> str | None:
    """Read one line from the controlling terminal."""
    errors = []
    tty_fd = os.environ.get("SIGIL_TTY_FD")
    if tty_fd:
        try:
            fd = os.dup(int(tty_fd))
            try:
                return prompt_on_fd(fd, prompt)
            finally:
                os.close(fd)
        except (OSError, ValueError) as exc:
            errors.append(f"fd {tty_fd}: {exc}")

    tty_paths = confirmation_tty_paths()
    for tty_path in tty_paths:
        try:
            fd = os.open(tty_path, os.O_RDWR)
            try:
                return prompt_on_fd(fd, prompt)
            finally:
                os.close(fd)
        except OSError as exc:
            errors.append(f"{tty_path}: {exc}")
            continue
    tried = [f"fd {tty_fd}"] if tty_fd else []
    tried.extend(tty_paths)
    detail = f"; errors: {'; '.join(errors)}" if errors else ""
    print(
        "sigil: could not open a terminal for confirmation; "
        f"tried {', '.join(tried)}{detail}; declining",
        file=sys.stderr,
    )
    return None


def prompt_on_fd(fd: int, prompt: str) -> str:
    """Read one line from an already-open terminal-like file descriptor."""
    os.write(fd, prompt.encode("utf-8"))
    answer = os.read(fd, 1024)
    return answer.decode("utf-8", errors="replace")


def clear_lines_on_tty(count: int) -> None:
    """Erase the last ``count`` lines from the controlling terminal."""
    if count <= 0:
        return
    sys.stdout.flush()
    fd = open_tty_fd()
    if fd is None:
        return
    try:
        if not os.isatty(fd):
            return
        os.write(fd, f"\033[{count}A\r\033[J".encode("utf-8"))
    finally:
        os.close(fd)


def open_tty_fd() -> int | None:
    """Open the controlling terminal for writing; return its fd or None."""
    tty_fd = os.environ.get("SIGIL_TTY_FD")
    if tty_fd:
        try:
            return os.dup(int(tty_fd))
        except (OSError, ValueError):
            pass
    for tty_path in confirmation_tty_paths():
        try:
            return os.open(tty_path, os.O_RDWR)
        except OSError:
            continue
    return None


def confirmation_tty_paths() -> list[str]:
    """Return candidate terminal devices for interactive confirmation."""
    paths = []
    for name in ("SIGIL_TTY", "TTY"):
        value = os.environ.get(name)
        if value:
            paths.append(value)
    paths.append("/dev/tty")
    return list(dict.fromkeys(paths))


CAPTURE_READ_SIZE = 65536


def open_capture_pty(reference_fd: int | None = None) -> tuple[int, int, str]:
    """Open a pty for turn capture, sized to the real terminal.

    Returns ``(master_fd, slave_fd, slave_path)``. The shell redirects a command's
    stdout or stderr onto ``slave_path`` so the command still sees a tty, while a
    relay drains ``master_fd``. ``reference_fd`` provides the window size when it
    is itself a terminal.
    """
    master_fd, slave_fd = pty.openpty()
    set_transparent_output(slave_fd)
    apply_terminal_winsize(slave_fd, reference_fd)
    return master_fd, slave_fd, os.ttyname(slave_fd)


def set_transparent_output(slave_fd: int) -> None:
    """Disable pty output post-processing so captured bytes pass through verbatim.

    Without this the slave maps ``\\n`` to ``\\r\\n`` (ONLCR); the real terminal we
    mirror to applies its own processing, so the capture pty must stay transparent.
    """
    try:
        attrs = termios.tcgetattr(slave_fd)
    except termios.error:
        return
    attrs[1] &= ~termios.OPOST
    try:
        termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
    except termios.error:
        pass


def apply_terminal_winsize(slave_fd: int, reference_fd: int | None = None) -> None:
    """Copy a window size onto a capture pty slave from the reference or terminal."""
    size = fd_winsize(reference_fd) if reference_fd is not None else None
    if size is None:
        size = terminal_winsize()
    if size is None:
        return
    try:
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, size)
    except OSError:
        pass


def fd_winsize(fd: int) -> bytes | None:
    """Return the packed winsize struct for a terminal fd, or None."""
    try:
        return fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)
    except OSError:
        return None


def terminal_winsize() -> bytes | None:
    """Return the real terminal's packed winsize struct, or None when unavailable."""
    for path in confirmation_tty_paths():
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOCTTY)
        except OSError:
            continue
        try:
            return fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)
        except OSError:
            continue
        finally:
            os.close(fd)
    return None


def relay_capture(
    master_fd: int,
    sink_path: str,
    *,
    mirror_fd: int | None,
    should_stop: Callable[[], bool],
    slave_fd: int | None = None,
    grace: float = 0.5,
    poll_interval: float = 0.1,
) -> None:
    """Mirror pty-master output to ``mirror_fd`` and a sink file until stopped.

    Reads ``master_fd`` and writes every byte to both ``mirror_fd`` (the command's
    original output stream) and ``sink_path`` (the captured transcript). The reader
    holds ``slave_fd`` open just long enough (until the first read or ``grace``
    seconds) for the shell to take over the slave, then closes it so a later
    shell-side close — from stopping capture or the shell exiting — surfaces as a
    master EOF and ends the relay. ``should_stop`` (SIGTERM) forces an early stop.
    """
    started = time.monotonic()
    with open(sink_path, "ab", buffering=0) as sink:
        while not should_stop():
            readable = relay_readable(master_fd, poll_interval)
            if slave_fd is not None and (
                readable or time.monotonic() - started >= grace
            ):
                os.close(slave_fd)
                slave_fd = None
            if not readable:
                continue
            chunk = relay_read(master_fd)
            if chunk is None:
                break
            if chunk:
                relay_emit(chunk, sink, mirror_fd)
        relay_drain(master_fd, sink, mirror_fd)
    if slave_fd is not None:
        os.close(slave_fd)


def open_tty_mirror(tty_path: str | None) -> int | None:
    """Open the terminal device for mirrored output, or None when unavailable.

    Writing to the device path is immune to in-shell fd redirection (prompt
    frameworks, loggers), so captured output still reaches the screen.
    """
    if not tty_path:
        return None
    try:
        return os.open(tty_path, os.O_WRONLY | os.O_NOCTTY)
    except OSError:
        return None


def relay_readable(fd: int, timeout: float) -> bool:
    """Return whether ``fd`` has data, waiting at most ``timeout`` seconds."""
    try:
        readable, _, _ = select.select([fd], [], [], timeout)
    except (InterruptedError, OSError):
        return False
    return bool(readable)


def relay_read(fd: int) -> bytes | None:
    """Read one chunk; bytes on data, empty on transient retry, None on hangup.

    An empty ``os.read`` means EOF (the shell closed the slave); a pty hangup can
    instead raise ``OSError`` (EIO on Linux). Both map to None so the relay stops.
    """
    try:
        data = os.read(fd, CAPTURE_READ_SIZE)
    except (BlockingIOError, InterruptedError):
        return b""
    except OSError:
        return None
    return data if data else None


def relay_drain(master_fd: int, sink: BinaryIO, mirror_fd: int | None) -> None:
    """Flush whatever remains in the master after the command has exited."""
    os.set_blocking(master_fd, False)
    while True:
        try:
            chunk = os.read(master_fd, CAPTURE_READ_SIZE)
        except (BlockingIOError, InterruptedError, OSError):
            break
        if not chunk:
            break
        relay_emit(chunk, sink, mirror_fd)


def relay_emit(chunk: bytes, sink: BinaryIO, mirror_fd: int | None) -> None:
    """Write one chunk to the sink file and, when available, the original stream."""
    sink.write(chunk)
    if mirror_fd is not None:
        try:
            os.write(mirror_fd, chunk)
        except OSError:
            pass
