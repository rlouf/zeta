from __future__ import annotations
import pytest
import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def make_stub(tmp: Path) -> Path:
    stub = tmp / "sigil-stub"
    stub.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            printf '%s\n' "$*" >> "$SIGIL_STUB_LOG"
            case "$*" in
              "command --select hello") printf '%s\n' "echo generated" ;;
              "command draft executive summary") printf '%s\n' "stream command" ;;
              "fix") printf '%s\n' "echo fix" ;;
              "ask hello") printf '%s\n' "answer" ;;
              "ask --follow-up hello") printf '%s\n' "follow-up" ;;
              "ask --follow-up review risky changes") printf '%s\n' "stream follow-up" ;;
              "op , hello") printf '%s\n%s\n' "echo recommended" "because it is safe" ;;
              "op , draft executive summary") printf '%s\n%s\n' "echo stream recommended" "because stdin matters" ;;
              op*) printf '%s\n' "op:$*" ;;
              record-failure*) printf '%s\n' "recorded" ;;
              *) printf '%s\n' "unexpected:$*" >&2; exit 64 ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return stub


def run_shell(
    shell: str, script: str, tmp: Path, stub: Path
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["SIGIL_BIN"] = str(stub)
    env["SIGIL_STUB_LOG"] = str(tmp / "calls.log")
    env["SIGIL_SESSION_ID"] = "shell-test"
    env["ZLE_LOG"] = str(tmp / "zle.log")
    return subprocess.run(
        [shell, "-c", script],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def assert_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def read_log(tmp: Path) -> list[str]:
    path = tmp / "calls.log"
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def test_bash_wrappers_call_current_cli_contract() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    source shell/bash/sigil.bash\n                    sigil_command hello\n                    sigil_execute_command hello\n                    sigil_question hello\n                    sigil_follow_up hello\n                    sigil_fix\n                    sigil_deep_fix\n                    printf 'history=%s\\n' \"$(__sigil_history_line)\"\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            "op , hello",
            "op ,, hello",
            "op ? hello",
            "ask --follow-up hello",
            "op ^",
            "op ^^",
        ]
        assert "echo recommended" in result.stdout
        assert "because it is safe" in result.stdout
        assert "op:op ,, hello" in result.stdout
        assert "op:op ^" in result.stdout
        assert "op:op ^^" in result.stdout
        assert "history=echo recommended" in result.stdout


def test_bash_recommendations_print_stdout_and_command_to_history() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    source shell/bash/sigil.bash\n                    sigil_command hello\n                    printf 'history=%s\\n' \"$(__sigil_history_line)\"\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert result.stdout == (
            "echo recommended\nbecause it is safe\nhistory=echo recommended\n"
        )


def test_bash_exports_tty_for_pipeline_confirmations() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    unset SIGIL_TTY\n                    export TTY=/tmp/sigil-test-tty\n                    source shell/bash/sigil.bash\n                    printf 'sigil_tty=%s\\n' \"$SIGIL_TTY\"\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert result.stdout == "sigil_tty=/tmp/sigil-test-tty\n"


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_exports_tty_for_pipeline_confirmations() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                '                    unset SIGIL_TTY\n                    export TTY=/tmp/sigil-test-tty\n                    source shell/zsh/sigil.zsh\n                    print -- "sigil_tty=$SIGIL_TTY"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert result.stdout == "sigil_tty=/tmp/sigil-test-tty\n"


def test_bash_wrappers_dispatch_piped_stdin_to_operator_runtime() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    source shell/bash/sigil.bash\n                    printf 'diff\\n' | sigil_follow_up review risky changes\n                    printf 'notes\\n' | sigil_command draft executive summary\n                    printf 'cmd\\n' | sigil_execute_command run it\n                    printf 'files\\n' | sigil_deep_fix rename symbol\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            "ask --follow-up review risky changes",
            "op , draft executive summary",
            "op ,, run it",
            "op ^^ rename symbol",
        ]
        assert "echo stream recommended" in result.stdout
        assert "because stdin matters" in result.stdout


def test_bash_records_failed_non_sigil_history_entries() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    source shell/bash/sigil.bash\n                    __sigil_history_line() { printf '%s\\n' \"bad command\"; }\n                    false\n                    __sigil_precmd\n                    __sigil_history_line() { printf '%s\\n' \", should not record\"; }\n                    false\n                    __sigil_precmd\n                    :\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [f"record-failure --status 1 --cwd {ROOT} bad command"]


def test_bash_does_not_record_failed_sigil_commands() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    source shell/bash/sigil.bash\n                    __sigil_history_line() { printf '%s\\n' \"sigil bad\"; }\n                    false\n                    __sigil_precmd\n                    __sigil_history_line() { printf '%s\\n' \"^\"; }\n                    false\n                    __sigil_precmd\n                    :\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == []


def test_bash_passes_failure_snippet_env_when_present() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                '                    source shell/bash/sigil.bash\n                    __sigil_history_line() { printf \'%s\\n\' "bad command"; }\n                    export SIGIL_FAILURE_STDOUT="stdout line"\n                    export SIGIL_FAILURE_STDERR="stderr line"\n                    false\n                    __sigil_precmd\n                    :\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            f"record-failure --status 1 --cwd {ROOT} --stdout-snippet stdout line --stderr-snippet stderr line bad command"
        ]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_wrappers_call_current_cli_contract() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                '                    source shell/zsh/sigil.zsh\n                    sigil_command hello\n                    sigil_execute_command hello\n                    sigil_question hello\n                    sigil_follow_up hello\n                    sigil_fix\n                    sigil_deep_fix\n                    print -- "history=${history[$HISTCMD]}"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            "op , hello",
            "op ,, hello",
            "op ? hello",
            "ask --follow-up hello",
            "op ^",
            "op ^^",
        ]
        assert "echo recommended" in result.stdout
        assert "because it is safe" in result.stdout
        assert "op:op ,, hello" in result.stdout
        assert "op:op ^" in result.stdout
        assert "op:op ^^" in result.stdout
        assert "history=echo recommended" in result.stdout


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_wrappers_dispatch_piped_stdin_to_operator_runtime() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                "                    source shell/zsh/sigil.zsh\n                    printf 'diff\\n' | sigil_follow_up review risky changes\n                    printf 'notes\\n' | sigil_command draft executive summary\n                    printf 'cmd\\n' | sigil_execute_command run it\n                    printf 'files\\n' | sigil_deep_fix rename symbol\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            "ask --follow-up review risky changes",
            "op , draft executive summary",
            "op ,, run it",
            "op ^^ rename symbol",
        ]
        assert "stream follow-up" in result.stdout
        assert "echo stream recommended" in result.stdout
        assert "because stdin matters" in result.stdout
        assert "op:op ,, run it" in result.stdout
        assert "op:op ^^ rename symbol" in result.stdout


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_glyph_aliases_dispatch_piped_stdin_before_globbing() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                "                    source shell/zsh/sigil.zsh\n                    eval \"printf 'diff\\\\n' | ?? review risky changes\"\n                    eval \"printf 'notes\\\\n' | , draft executive summary\"\n                    eval \"printf 'cmd\\\\n' | ,, run it\"\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            "ask --follow-up review risky changes",
            "op , draft executive summary",
            "op ,, run it",
        ]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_repairs_print_stdout_not_history() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                '                    source shell/zsh/sigil.zsh\n                    sigil_fix\n                    print -- "history=${history[$HISTCMD]}"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == ["op ^"]
        assert "op:op ^" in result.stdout


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_does_not_record_failed_sigil_commands() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                '                    source shell/zsh/sigil.zsh\n                    __sigil_preexec "sigil bad"\n                    false\n                    __sigil_precmd\n                    __sigil_preexec "^"\n                    false\n                    __sigil_precmd\n                    :\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == []
