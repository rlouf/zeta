from __future__ import annotations

import errno
import os
import pty
import select
import shutil
import signal
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHELL_TIMEOUT_SECONDS = 60.0


def make_stub(tmp: Path) -> Path:
    stub = tmp / "sigil-stub"
    stub.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            if [ "$1" = "step" ]; then
              continue_step=0
              handoff_file=""
              objective=""
              argc=0
              while [ "$#" -gt 0 ]; do
                case "$1" in
                  --handoff-file)
                    handoff_file="$2"
                    shift 2
                    ;;
                  --workflow)
                    shift 2
                    ;;
                  --continue)
                    continue_step=1
                    shift
                    ;;
                  step)
                    shift
                    ;;
                  *)
                    objective="$1"
                    argc=$((argc + 1))
                    shift
                    ;;
                esac
              done
              if [ "$continue_step" = "1" ]; then
                printf '%s\n' "step --continue argc=$argc" >> "$SIGIL_STUB_LOG"
                command="echo continued"
                reason="Continue after shell handoff."
              else
                printf '%s\n' "step" >> "$SIGIL_STUB_LOG"
                case "$objective" in
                  *interrupt*) kill -INT $PPID; kill -INT $$ ;;
                esac
                case "$objective" in
                  *repair*) command="uv run pytest"; reason="Run tests." ;;
                  *"run it"*) command="echo piped"; reason="Run piped handoff." ;;
                  *) command="echo zeta"; reason="Run zeta handoff." ;;
                esac
              fi
              printf '❯ bash   %s  (staged)\n' "$command"
              if [ -n "$handoff_file" ]; then
                printf '%s\n' "$command" > "$handoff_file"
              fi
              exit 0
            fi
            printf '%s\n' "$*" >> "$SIGIL_STUB_LOG"
            case "$*" in
              "command draft executive summary") printf '%s\n' "stream command" ;;
              "ask hello") printf '%s\n' "answer" ;;
              "ask draft executive summary") printf '%s\n' "readonly stream answer" ;;
              status*) printf '%s\n' "clean" ;;
              run*) printf '%s\n' "ran:${*:2}" ;;
              *) printf '%s\n' "unexpected:$*" >&2; exit 64 ;;
            esac
            """
        ).lstrip(),
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def shell_env(tmp: Path, stub: Path) -> dict[str, str]:
    """Deterministic environment for binding tests.

    Built from scratch rather than copied from os.environ, so developer
    shell state (sigil variables, rc exports, terminal config) cannot
    change the behavior under test. PATH is inherited to locate zsh,
    bash, and system tools.
    """
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp),
        "TMPDIR": str(tmp),
        "SIGIL_BIN": str(stub),
        "SIGIL_STUB_LOG": str(tmp / "calls.log"),
        "SIGIL_SESSION_ID": "shell-test",
        "SIGIL_STATE_DIR": str(tmp / "state"),
        "ZLE_LOG": str(tmp / "zle.log"),
    }


def run_shell(
    shell: str, script: str, tmp: Path, stub: Path
) -> subprocess.CompletedProcess[str]:
    env = shell_env(tmp, stub)
    return subprocess.run(
        [shell, "-c", script],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=SHELL_TIMEOUT_SECONDS,
    )


def run_shell_args(
    args: list[str], script: str, tmp: Path, stub: Path
) -> subprocess.CompletedProcess[str]:
    env = shell_env(tmp, stub)
    return subprocess.run(
        [*args, script],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=SHELL_TIMEOUT_SECONDS,
    )


def run_shell_stdin(
    args: list[str], script: str, tmp: Path, stub: Path
) -> subprocess.CompletedProcess[str]:
    env = shell_env(tmp, stub)
    return subprocess.run(
        args,
        input=script,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=SHELL_TIMEOUT_SECONDS,
    )


def run_shell_pty(
    shell: str,
    script: str,
    tmp: Path,
    stub: Path,
    timeout_seconds: float = SHELL_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    env = shell_env(tmp, stub)

    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(ROOT)
        os.environ.clear()
        os.environ.update(env)
        os.execlp(shell, shell, "-f", "-i")

    os.write(fd, script.encode())
    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            os.close(fd)
            raise TimeoutError(
                f"pty shell still running after {timeout_seconds}s; "
                f"output so far:\n{b''.join(chunks).decode(errors='replace')}"
            )
        ready, _, _ = select.select([fd], [], [], min(remaining, 1.0))
        if not ready:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError as exc:
            if exc.errno == errno.EIO:
                break
            raise
        if not chunk:
            break
        chunks.append(chunk)
    _, status = os.waitpid(pid, 0)
    os.close(fd)
    return subprocess.CompletedProcess(
        [shell, "-c", script],
        os.waitstatus_to_exitcode(status),
        b"".join(chunks).decode(errors="replace"),
        "",
    )


class InteractiveZsh:
    """Drive one interactive zsh over a pty: send lines, await markers.

    The prompt is set to a sentinel after spawn so tests can wait for "the
    shell is back at the prompt" instead of sleeping. The sentinel is split
    when sent so the echoed assignment never matches an expect() for it.
    """

    PROMPT = "SIGIL_PTY_PROMPT> "

    def __init__(
        self,
        tmp: Path,
        stub: Path,
        env: dict[str, str] | None = None,
    ) -> None:
        full_env = shell_env(tmp, stub)
        if env:
            full_env.update(env)
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(ROOT)
            os.environ.clear()
            os.environ.update(full_env)
            os.execlp("zsh", "zsh", "-f", "-i")
        self.output = ""
        self.scanned = 0
        self.closed = False
        head, tail = self.PROMPT[:5], self.PROMPT[5:]
        self.sendline(f"PS1='{head}''{tail}'; RPS1=''")
        self.expect_prompt()

    def send(self, data: str) -> None:
        os.write(self.fd, data.encode())

    def sendline(self, line: str) -> None:
        self.send(line + "\n")

    def send_control(self, letter: str) -> None:
        self.send(chr(ord(letter.upper()) - ord("A") + 1))

    def expect(self, needle: str, timeout_seconds: float = 30.0) -> None:
        """Consume output until ``needle`` appears past the last match."""
        deadline = time.monotonic() + timeout_seconds
        while True:
            found = self.output.find(needle, self.scanned)
            if found != -1:
                self.scanned = found + len(needle)
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.kill()
                raise TimeoutError(
                    f"never saw {needle!r}; output so far:\n{self.output}"
                )
            ready, _, _ = select.select([self.fd], [], [], min(remaining, 1.0))
            if not ready:
                continue
            try:
                chunk = os.read(self.fd, 4096)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    raise TimeoutError(
                        f"shell exited before {needle!r}; output:\n{self.output}"
                    ) from exc
                raise
            if not chunk:
                raise TimeoutError(
                    f"shell exited before {needle!r}; output:\n{self.output}"
                )
            self.output += chunk.decode(errors="replace")

    def expect_prompt(self, timeout_seconds: float = 30.0) -> None:
        self.expect(self.PROMPT, timeout_seconds)

    def run(self, line: str, timeout_seconds: float = 30.0) -> None:
        """Send one line and wait for the next prompt."""
        self.sendline(line)
        self.expect_prompt(timeout_seconds)

    def exit(self) -> int:
        self.sendline("exit")
        while True:
            ready, _, _ = select.select([self.fd], [], [], 1.0)
            if not ready:
                continue
            try:
                chunk = os.read(self.fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            self.output += chunk.decode(errors="replace")
        return self.close()

    def kill(self) -> None:
        if not self.closed:
            os.kill(self.pid, signal.SIGKILL)
            self.close()

    def close(self) -> int:
        if self.closed:
            return 0
        self.closed = True
        _, status = os.waitpid(self.pid, 0)
        os.close(self.fd)
        return os.waitstatus_to_exitcode(status)


def assert_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def read_log(tmp: Path) -> list[str]:
    path = tmp / "calls.log"
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def step_calls() -> list[str]:
    return ["step"]


def shell_turn_calls(tmp: Path) -> list[dict[str, str]]:
    """Parse the recording spool the binding appends to with zero forks."""
    path = tmp / "state" / "sessions" / "shell-test" / "shell-turns.spool"
    if not path.exists():
        return []
    records = []
    for record in path.read_text(encoding="utf-8").split("\x1e"):
        fields = record.split("\x1f")
        if len(fields) == 4:
            records.append(
                {
                    "time": fields[0],
                    "command": fields[1],
                    "status": fields[2],
                    "cwd": fields[3],
                }
            )
    return records


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_wrappers_call_current_cli_contract() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                '                    source src/sigil/bindings/sigil.zsh\n                    sigil_command hello\n                    sigil_agent_step hello\n                    print -- "history=${history[$HISTCMD]}"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            "ask hello",
            *step_calls(),
        ]
        assert "answer" in result.stdout
        assert "❯ bash   echo zeta  (staged)" in result.stdout
        assert "Run zeta handoff." not in result.stdout
        assert "history=+ echo zeta" in result.stdout


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_agent_step_uses_zeta_handoff_directly() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                '                    source src/sigil/bindings/sigil.zsh\n                    sigil_agent_step_auto repair\n                    print -- "history=${history[$HISTCMD]}"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert "(staged)" in result.stdout
        assert "Run tests." not in result.stdout
        assert "history=+ uv run pytest" in result.stdout


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_bare_agent_step_continues_after_shell_handoff() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                '                    source src/sigil/bindings/sigil.zsh\n                    sigil_agent_step\n                    print -- "history=${history[$HISTCMD]}"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        # argc=0: a bare continue passes no positional, not an empty string the
        # CLI has to know to ignore.
        assert read_log(tmp) == ["step --continue argc=0"]
        assert "(staged)" in result.stdout
        assert "Continue after shell handoff." not in result.stdout
        assert "history=+ echo continued" in result.stdout


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_agent_wrappers_call_zeta_loop() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                "                    source src/sigil/bindings/sigil.zsh\n                    sigil_agent_step hello\n                    sigil_agent_step_auto hello\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            *step_calls(),
            *step_calls(),
        ]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_wrappers_dispatch_piped_stdin_to_operator_runtime() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                "                    source src/sigil/bindings/sigil.zsh\n                    printf 'notes\\n' | sigil_command draft executive summary\n                    printf 'cmd\\n' | sigil_agent_step run it\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            "ask draft executive summary",
            *step_calls(),
        ]
        assert "readonly stream answer" in result.stdout
        assert "(staged)" in result.stdout
        assert "Run piped handoff." not in result.stdout


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_glyph_aliases_dispatch_piped_stdin_before_globbing() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                "                    source src/sigil/bindings/sigil.zsh\n                    eval \"printf 'notes\\\\n' | , draft executive summary\"\n                    eval \"printf 'cmd\\\\n' | ,, run it\"\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            "ask draft executive summary",
            *step_calls(),
        ]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_does_not_record_sigil_commands() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                "                    source src/sigil/bindings/sigil.zsh\n                    false\n                    wait\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == []
        assert shell_turn_calls(tmp) == []


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_does_not_record_sigil_wrapper_commands() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                "                    source src/sigil/bindings/sigil.zsh\n                    sigil_command hello\n                    wait\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert result.stdout == "answer\n"
        assert read_log(tmp) == ["ask hello"]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_plus_line_goes_through_capture_widget() -> None:
    # End to end through zle: the accept-line widget captures the raw line and
    # hands it to `sigil run --shell` before zsh parses it.
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell_pty(
            "zsh",
            textwrap.dedent(
                """\
                source src/sigil/bindings/sigil.zsh
                + echo captured
                exit
                """
            ),
            tmp,
            stub,
        )
        assert "ran:--shell echo captured" in result.stdout
        assert read_log(tmp) == ["run --shell echo captured"]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_plus_glyph_is_widget_only() -> None:
    # No alias or function fallback: outside zle the + glyph does not dispatch
    # at all instead of silently switching to argv parsing, where zsh would
    # split pipes and redirections itself.
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                "                    source src/sigil/bindings/sigil.zsh\n                    + echo captured\n                    "
            ),
            tmp,
            stub,
        )
        assert result.returncode != 0
        assert read_log(tmp) == []


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_raw_plus_capture_dispatches_shell_command_to_sigil_run() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                '                    source src/sigil/bindings/sigil.zsh\n                    __sigil_run_plus_capture_line "+ echo captured | cat"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert result.stdout == "ran:--shell echo captured | cat\n"
        assert read_log(tmp) == ["run --shell echo captured | cat"]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_raw_plus_capture_handles_multiline_buffers() -> None:
    # A staged multiline command arrives in the buffer as one accept-line
    # event; the whole buffer must reach sigil run instead of falling through
    # to zsh, which would execute the tail lines as plain commands.
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                """\
                source src/sigil/bindings/sigil.zsh
                __sigil_run_plus_capture_line "+ echo one
                echo two"
                """
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == ["run --shell echo one", "echo two"]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_installs_raw_plus_capture_accept_line_widget() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell_args(
            ["zsh", "-f", "-ic"],
            textwrap.dedent(
                '                    source src/sigil/bindings/sigil.zsh\n                    print -- "widget=${widgets[accept-line]}"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert "widget=user:__sigil_accept_line_with_plus_capture" in result.stdout


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_accept_line_plus_capture_preserves_exit_status() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                '                    function zle() { return 0; }\n                    source src/sigil/bindings/sigil.zsh\n                    BUFFER="+ echo captured"\n                    __sigil_accept_line_with_plus_capture\n                    print -- "exit=$?"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert result.stderr == ""
        assert "read-only variable: status" not in result.stdout
        assert result.stdout == "\nran:--shell echo captured\nexit=0\n"
        assert read_log(tmp) == ["run --shell echo captured"]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_wraps_simple_zeta_handoff_with_run_capture() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                '                    source src/sigil/bindings/sigil.zsh\n                    sigil_agent_step hello >/dev/null\n                    print -- "history=${history[$HISTCMD]}"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert result.stdout == "history=+ echo zeta\n"


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_wraps_shell_grammar_handoff_with_run_capture() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                '                    source src/sigil/bindings/sigil.zsh\n                    __sigil_history_insert "$(__sigil_zeta_prompt_command "echo zeta | cat")"\n                    print -- "history=${history[$HISTCMD]}"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert result.stdout == "history=+ echo zeta | cat\n"


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_status_glyph_dispatches_to_sigil_status() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                # eval re-parses with the sourced aliases in scope, matching the
                # line-at-a-time parsing of an interactive shell. A plain `?` in
                # a fully pre-parsed `zsh -c` script never sees the alias.
                "                    source src/sigil/bindings/sigil.zsh\n                    eval '?'\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert result.stdout == "clean\n"
        assert read_log(tmp) == ["status"]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_binding_preserves_question_mark_globbing() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        (tmp / "ab").touch()
        result = run_shell(
            "zsh",
            textwrap.dedent(
                f"""\
                source src/sigil/bindings/sigil.zsh
                cd {tmp}
                print -- ?b
                """
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert result.stdout == "ab\n"


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_interrupted_zeta_step_leaves_no_handoff_file() -> None:
    # Ctrl-C delivers SIGINT to the whole foreground process group, so zsh
    # itself aborts __sigil_step_turn mid-flight. The stub models that by
    # signalling its parent shell and itself. An interactive shell is required:
    # non-interactive zsh dies on SIGINT instead of unwinding to the prompt.
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell_stdin(
            ["zsh", "-f", "-i"],
            textwrap.dedent(
                f"""\
                export TMPDIR={tmp}
                source src/sigil/bindings/sigil.zsh
                sigil_agent_step interrupt
                print -- "survived=yes"
                """
            ),
            tmp,
            stub,
        )
        assert "survived=yes" in result.stdout
        assert list(tmp.glob("sigil-handoff.*")) == []


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_precmd_hook_runs_before_earlier_registered_hooks() -> None:
    # $? at precmd entry is only the user command's status for the first hook
    # in precmd_functions; hooks registered by plugins sourced before sigil
    # would otherwise clobber it.
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                """\
                autoload -Uz add-zsh-hook
                theme_precmd() { true }
                add-zsh-hook precmd theme_precmd
                source src/sigil/bindings/sigil.zsh
                source src/sigil/bindings/sigil.zsh
                print -- "hooks=$precmd_functions"
                """
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert (
            "hooks=__sigil_zeta_after_command_before_prompt theme_precmd"
            in result.stdout
        )


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_records_shell_turns_without_a_handoff() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                """\
                source src/sigil/bindings/sigil.zsh
                __sigil_zeta_before_command "echo recorded"
                true
                __sigil_zeta_after_command_before_prompt
                wait
                """
            ),
            tmp,
            stub,
        )
        assert_success(result)
        calls = shell_turn_calls(tmp)
        assert len(calls) == 1
        assert calls[0]["command"] == "echo recorded"
        assert calls[0]["status"] == "0"
        assert calls[0]["cwd"] == str(ROOT)
        assert float(calls[0]["time"]) > 0


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_records_every_command_with_no_turn_limit() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                """\
                source src/sigil/bindings/sigil.zsh
                for command in "echo one" "echo two" "echo three"; do
                  __sigil_zeta_before_command "$command"
                  true
                  __sigil_zeta_after_command_before_prompt
                done
                wait
                """
            ),
            tmp,
            stub,
        )
        assert_success(result)
        calls = shell_turn_calls(tmp)
        assert [call["command"] for call in calls] == [
            "echo one",
            "echo two",
            "echo three",
        ]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_leading_space_skips_recording() -> None:
    # Privacy parity with zsh's ignorespace convention: a command typed with
    # a leading space leaves no record.
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                """\
                source src/sigil/bindings/sigil.zsh
                __sigil_zeta_before_command " echo secret"
                true
                __sigil_zeta_after_command_before_prompt
                __sigil_zeta_before_command "echo recorded"
                true
                __sigil_zeta_after_command_before_prompt
                wait
                """
            ),
            tmp,
            stub,
        )
        assert_success(result)
        calls = shell_turn_calls(tmp)
        assert len(calls) == 1
        assert calls[0]["command"] == "echo recorded"


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_sigil_record_opt_out_disables_recording() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                """\
                source src/sigil/bindings/sigil.zsh
                export SIGIL_RECORD=0
                __sigil_zeta_before_command "echo zero"
                true
                __sigil_zeta_after_command_before_prompt
                export SIGIL_RECORD=false
                __sigil_zeta_before_command "echo false"
                true
                __sigil_zeta_after_command_before_prompt
                export SIGIL_RECORD=1
                __sigil_zeta_before_command "echo recorded"
                true
                __sigil_zeta_after_command_before_prompt
                wait
                """
            ),
            tmp,
            stub,
        )
        assert_success(result)
        calls = shell_turn_calls(tmp)
        assert len(calls) == 1
        assert calls[0]["command"] == "echo recorded"


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_records_interactive_commands_end_to_end() -> None:
    # Through a real interactive shell: preexec/precmd fire on their own and
    # the recorded turn reaches the CLI before the next prompt.
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        run_shell_pty(
            "zsh",
            textwrap.dedent(
                """\
                source src/sigil/bindings/sigil.zsh
                echo hi
                exit
                """
            ),
            tmp,
            stub,
        )
        calls = shell_turn_calls(tmp)
        assert len(calls) == 1
        assert calls[0]["command"] == "echo hi"
        assert calls[0]["status"] == "0"


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_recordable_command_excludes_all_sigil_invocations() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                """\
                source src/sigil/bindings/sigil.zsh
                __sigil_zeta_recordable_command "sigil"; print -- "bare=$?"
                __sigil_zeta_recordable_command "sigil status"; print -- "args=$?"
                __sigil_zeta_recordable_command "./sigil status"; print -- "relative=$?"
                __sigil_zeta_recordable_command "/usr/local/bin/sigil status"; print -- "absolute=$?"
                __sigil_zeta_recordable_command " echo hi"; print -- "space=$?"
                __sigil_zeta_recordable_command "echo sigil"; print -- "mention=$?"
                __sigil_zeta_recordable_command "echo hi"; print -- "plain=$?"
                """
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert "bare=1" in result.stdout
        assert "args=1" in result.stdout
        assert "relative=1" in result.stdout
        assert "absolute=1" in result.stdout
        assert "space=1" in result.stdout
        assert "mention=0" in result.stdout
        assert "plain=0" in result.stdout


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_generates_session_id_without_uuidgen() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                """\
                unset SIGIL_SESSION_ID
                function uuidgen() { return 127 }
                source src/sigil/bindings/sigil.zsh
                print -- "sid=$SIGIL_SESSION_ID"
                """
            ),
            tmp,
            stub,
        )
        assert_success(result)
        sid = result.stdout.split("sid=", 1)[1].strip()
        assert sid
        assert "uuidgen" not in sid


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_resolves_cli_from_commands_hash_without_forking() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        shutil.copy(stub, bin_dir / "sigil")
        (bin_dir / "sigil").chmod(0o755)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                f"""\
                unset SIGIL_BIN
                path=({bin_dir} $path)
                source src/sigil/bindings/sigil.zsh
                print -- "bin=$__sigil_bin"
                """
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert f"bin={bin_dir}/sigil" in result.stdout


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_shell_turn_recording_does_not_spawn_python3() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                """\
                source src/sigil/bindings/sigil.zsh
                function python3() { print -- "python3 used" >> "$ZLE_LOG"; return 127 }
                __sigil_zeta_record_shell_turn "echo hi" 3
                """
            ),
            tmp,
            stub,
        )
        assert_success(result)
        calls = shell_turn_calls(tmp)
        assert len(calls) == 1
        assert calls[0]["command"] == "echo hi"
        assert calls[0]["status"] == "3"
        assert read_log(tmp) == []
        assert not (tmp / "zle.log").exists()


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_zeta_handoff_staging_does_not_spawn_python3() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                """\
                source src/sigil/bindings/sigil.zsh
                function python3() { print -- "python3 used" >> "$ZLE_LOG"; return 127 }
                sigil_agent_step hello >/dev/null
                print -- "history=${history[$HISTCMD]}"
                """
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert "history=+ echo zeta" in result.stdout
        assert not (tmp / "zle.log").exists()


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_binding_functions_survive_hostile_user_options() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                """\
                setopt ksh_arrays sh_word_split
                source src/sigil/bindings/sigil.zsh
                __sigil_run_plus_capture_line "+ echo captured | cat"
                sigil_agent_step hello >/dev/null
                print -- "history=${history[$HISTCMD]}"
                """
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert "ran:--shell echo captured | cat" in result.stdout
        assert "history=+ echo zeta" in result.stdout
        assert read_log(tmp) == [
            "run --shell echo captured | cat",
            *step_calls(),
        ]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_glyph_lines_recallable_in_session_but_not_saved() -> None:
    # An interactive shell adds accepted lines through zshaddhistory; glyph
    # prompts must stay recallable with up-arrow (internal history) without
    # ending up in the history file. inc_append_history exercises the file
    # write inside the session, where the return-2 mark applies.
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        histfile = tmp / "histfile"
        result = run_shell_stdin(
            ["zsh", "-f", "-i"],
            textwrap.dedent(
                f"""\
                HISTFILE={histfile}
                HISTSIZE=100
                SAVEHIST=100
                setopt inc_append_history
                source src/sigil/bindings/sigil.zsh
                , hello
                true
                history 1
                """
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert ", hello" in result.stdout
        saved = histfile.read_text(encoding="utf-8")
        assert ", hello" not in saved
        assert "true" in saved


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_history_filter_is_additive_and_covers_glyphs() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell_args(
            ["zsh", "-f", "-ic"],
            textwrap.dedent(
                '                    function zshaddhistory() { print -- "user:$1" >> "$ZLE_LOG"; return 0; }\n                    source src/sigil/bindings/sigil.zsh\n                    print -- "hooks=$zshaddhistory_functions"\n                    zshaddhistory "echo hello"\n                    __sigil_zshaddhistory ", hello"; print -- "comma=$?"\n                    __sigil_zshaddhistory "? hello"; print -- "question=$?"\n                    __sigil_zshaddhistory "\\? hello"; print -- "escaped_question=$?"\n                    __sigil_zshaddhistory "+ echo"; print -- "run=$?"\n                    __sigil_zshaddhistory "@ hello"; print -- "at=$?"\n                    __sigil_zshaddhistory "echo hello"; print -- "echo=$?"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert "__sigil_zshaddhistory" in result.stdout
        # 2 keeps the line on the internal history list (up-arrow recall) while
        # keeping it out of the history file.
        assert "comma=2" in result.stdout
        assert "question=2" in result.stdout
        assert "escaped_question=0" in result.stdout
        assert "run=2" in result.stdout
        assert "at=0" in result.stdout
        assert "echo=0" in result.stdout
        assert (tmp / "zle.log").read_text(encoding="utf-8") == "user:echo hello\n"


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_pty_harness_kills_a_wedged_shell() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        with pytest.raises(TimeoutError):
            run_shell_pty(
                "zsh",
                "sleep 60\n",
                tmp,
                stub,
                timeout_seconds=1.0,
            )


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_interactive_plus_dispatches_pipeline_through_widget() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        shell = InteractiveZsh(tmp, stub)
        try:
            shell.run("source src/sigil/bindings/sigil.zsh")
            shell.sendline("+ echo captured | cat")
            shell.expect("ran:--shell echo captured | cat")
            shell.expect_prompt()
            shell.run('print -- "st=$?"')
            shell.exit()
        finally:
            shell.kill()
        assert "st=0" in shell.output
        assert read_log(tmp) == ["run --shell echo captured | cat"]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_interactive_comma_glyph_dispatches_to_ask() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        shell = InteractiveZsh(tmp, stub)
        try:
            shell.run("source src/sigil/bindings/sigil.zsh")
            shell.sendline(", hello")
            shell.expect("answer")
            shell.expect_prompt()
            shell.exit()
        finally:
            shell.kill()
        assert read_log(tmp) == ["ask hello"]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_interactive_status_glyph_dispatches_to_status() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        shell = InteractiveZsh(tmp, stub)
        try:
            shell.run("source src/sigil/bindings/sigil.zsh")
            shell.sendline("?")
            shell.expect("clean")
            shell.expect_prompt()
            shell.exit()
        finally:
            shell.kill()
        assert read_log(tmp) == ["status"]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_interactive_glyph_line_recallable_with_up_arrow() -> None:
    # Up-arrow recall must restore the glyph line and re-dispatch on Enter:
    # behavioral pin via the stub call log, not display scraping.
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        shell = InteractiveZsh(tmp, stub)
        try:
            shell.run("source src/sigil/bindings/sigil.zsh")
            shell.sendline(", hello")
            shell.expect("answer")
            shell.expect_prompt()
            shell.send("\x1b[A")
            shell.send("\n")
            shell.expect("answer")
            shell.expect_prompt()
            shell.exit()
        finally:
            shell.kill()
        assert read_log(tmp) == ["ask hello", "ask hello"]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_interactive_commands_recorded_in_order_with_status() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        shell = InteractiveZsh(tmp, stub)
        try:
            shell.run("source src/sigil/bindings/sigil.zsh")
            shell.run("true")
            shell.run("false")
            shell.exit()
        finally:
            shell.kill()
        calls = shell_turn_calls(tmp)
        assert len(calls) == 2
        assert calls[0]["command"] == "true"
        assert calls[0]["status"] == "0"
        assert calls[1]["command"] == "false"
        assert calls[1]["status"] == "1"


def interactive_session_vars(shell: InteractiveZsh) -> dict[str, str]:
    # The markers are split in the sent line so the input echo cannot match
    # the expects; only the printed output contains the joined forms.
    shell.run("source src/sigil/bindings/sigil.zsh")
    shell.sendline('print -- "si""d=${SIGIL_SESSION_ID}@tt""y=${SIGIL_SESSION_TTY}@"')
    shell.expect("sid=")
    start = shell.scanned
    shell.expect("@tty=")
    sid = shell.output[start : shell.scanned - len("@tty=")]
    start = shell.scanned
    shell.expect("@")
    tty = shell.output[start : shell.scanned - 1]
    shell.expect_prompt()
    return {"sid": sid, "tty": tty}


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_interactive_session_id_regenerates_on_foreign_tty() -> None:
    # The tmux server propagates one shell's exported pair to every pane; an
    # inherited id whose recorded tty is not this pty must not be reused.
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        shell = InteractiveZsh(
            tmp,
            stub,
            env={
                "SIGIL_SESSION_ID": "stale-pane-id",
                "SIGIL_SESSION_TTY": "/dev/ttyFAKE0",
            },
        )
        try:
            values = interactive_session_vars(shell)
            shell.exit()
        finally:
            shell.kill()
        assert values["sid"] != "stale-pane-id"
        assert values["sid"]
        assert values["tty"] != "/dev/ttyFAKE0"


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_interactive_session_id_kept_on_same_tty() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        shell = InteractiveZsh(tmp, stub)
        try:
            shell.run('export SIGIL_SESSION_TTY="$TTY"')
            shell.run("export SIGIL_SESSION_ID=keep-me")
            values = interactive_session_vars(shell)
            shell.exit()
        finally:
            shell.kill()
        assert values["sid"] == "keep-me"


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_interactive_session_id_kept_without_recorded_tty() -> None:
    # An id set without a recorded tty is a deliberate override (tests, user
    # config) and survives sourcing.
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        shell = InteractiveZsh(tmp, stub)
        try:
            values = interactive_session_vars(shell)
            shell.exit()
        finally:
            shell.kill()
        assert values["sid"] == "shell-test"
        assert values["tty"]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_interactive_two_ptys_get_distinct_sessions() -> None:
    # The tmux scenario end to end: pane B inherits pane A's exported pair
    # and must end up in its own session.
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        first = InteractiveZsh(tmp, stub, env={"SIGIL_SESSION_ID": ""})
        try:
            first_values = interactive_session_vars(first)
            second = InteractiveZsh(
                tmp,
                stub,
                env={
                    "SIGIL_SESSION_ID": first_values["sid"],
                    "SIGIL_SESSION_TTY": first_values["tty"],
                },
            )
            try:
                second_values = interactive_session_vars(second)
                second.exit()
            finally:
                second.kill()
            first.exit()
        finally:
            first.kill()
        assert first_values["sid"]
        assert second_values["sid"]
        assert second_values["sid"] != first_values["sid"]
        assert second_values["tty"] != first_values["tty"]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_interactive_harness_times_out_on_missing_marker() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        shell = InteractiveZsh(tmp, stub)
        try:
            with pytest.raises(TimeoutError):
                shell.expect("never-printed", timeout_seconds=1.0)
        finally:
            shell.kill()


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_shell_harness_does_not_inherit_developer_environment(monkeypatch) -> None:
    monkeypatch.setenv("SIGIL_DEV_LEAK", "leaked")
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            'printf "%s" "${SIGIL_DEV_LEAK:-clean}"',
            tmp,
            stub,
        )
        assert result.returncode == 0
        assert result.stdout == "clean"
