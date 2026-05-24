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
            '                #!/usr/bin/env bash\n                printf \'%s\\n\' "$*" >> "$SIGIL_STUB_LOG"\n                case "$*" in\n                  "command --select hello") printf \'%s\\n\' "echo generated" ;;\n                  "command --previous --select") printf \'%s\\n\' "echo previous" ;;\n                  "fix") printf \'%s\\n\' "echo fix" ;;\n                  "fix --previous") printf \'%s\\n\' "echo previous-fix" ;;\n                  "question hello") printf \'%s\\n\' "answer" ;;\n                  "question --follow-up hello") printf \'%s\\n\' "follow-up" ;;\n                  "summary") printf \'%s\\n\' "summary" ;;\n                  "summary now") printf \'%s\\n\' "summary now" ;;\n                  record-failure*) printf \'%s\\n\' "recorded" ;;\n                  *) printf \'%s\\n\' "unexpected:$*" >&2; exit 64 ;;\n                esac\n                '
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
                "                    source shell/bash/sigil.bash\n                    sigil_command hello\n                    sigil_previous_command\n                    sigil_question hello\n                    sigil_follow_up hello\n                    sigil_fix\n                    sigil_previous_fix\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            "command --select hello",
            "command --previous --select",
            "question hello",
            "question --follow-up hello",
            "fix",
            "fix --previous",
        ]
        assert "echo generated" in result.stdout
        assert "echo previous-fix" in result.stdout


def test_bash_readline_dispatch_inserts_proposals_without_executing() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                '                    source shell/bash/sigil.bash\n                    READLINE_LINE=", hello"\n                    READLINE_POINT=${#READLINE_LINE}\n                    __sigil_readline_dispatch >/tmp/sigil-shell-test.out\n                    printf \'command_buffer=%s\\n\' "$READLINE_LINE"\n\n                    READLINE_LINE="^^"\n                    READLINE_POINT=${#READLINE_LINE}\n                    __sigil_readline_dispatch >/tmp/sigil-shell-test.out\n                    printf \'fix_buffer=%s\\n\' "$READLINE_LINE"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == ["command --select hello", "fix --previous"]
        assert "command_buffer=echo generated" in result.stdout
        assert "fix_buffer=echo previous-fix" in result.stdout


def test_bash_blocks_execute_and_promotion_routes_before_cli() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                '                    source shell/bash/sigil.bash\n                    READLINE_LINE=",! rm -rf nope"\n                    __sigil_readline_dispatch >/tmp/sigil-shell-test.out\n                    printf \'bang_buffer=%s\\n\' "$READLINE_LINE"\n\n                    READLINE_LINE="@ promote"\n                    __sigil_readline_dispatch >/tmp/sigil-shell-test.out\n                    printf \'at_buffer=%s\\n\' "$READLINE_LINE"\n\n                    READLINE_LINE="?! run"\n                    __sigil_readline_dispatch >/tmp/sigil-shell-test.out\n                    printf \'question_bang_buffer=%s\\n\' "$READLINE_LINE"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == []
        assert "bang_buffer=" in result.stdout
        assert "at_buffer=" in result.stdout
        assert "question_bang_buffer=" in result.stdout


def test_bash_question_routes_clear_the_prompt_buffer() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                '                    source shell/bash/sigil.bash\n                    READLINE_LINE="?? hello"\n                    READLINE_POINT=${#READLINE_LINE}\n                    __sigil_readline_dispatch\n                    printf \'follow_up_buffer=%s\\n\' "$READLINE_LINE"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == ["question --follow-up hello"]
        assert "follow_up_buffer=" in result.stdout


def test_bash_summary_route_is_read_only_and_clears_the_prompt_buffer() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                '                    source shell/bash/sigil.bash\n                    READLINE_LINE="@. now"\n                    READLINE_POINT=${#READLINE_LINE}\n                    __sigil_readline_dispatch\n                    printf \'summary_buffer=%s\\n\' "$READLINE_LINE"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == ["summary now"]
        assert "summary_buffer=" in result.stdout


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
                "                    source shell/zsh/sigil.zsh\n                    sigil_command hello\n                    sigil_previous_command\n                    sigil_question hello\n                    sigil_follow_up hello\n                    sigil_fix >/tmp/sigil-zsh-fix.out\n                    sigil_previous_fix >/tmp/sigil-zsh-prev-fix.out\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            "command --select hello",
            "command --previous --select",
            "question hello",
            "question --follow-up hello",
            "fix",
            "fix --previous",
        ]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_command_routes_do_not_quote_the_visible_buffer() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                '                    function zle { print -- "$1:$BUFFER" >> "$ZLE_LOG"; }\n                    source shell/zsh/sigil.zsh\n                    BUFFER=", hello"\n                    __sigil_accept_line\n                    print -- "command_buffer=$BUFFER"\n                    BUFFER=",,"\n                    __sigil_accept_line\n                    print -- "previous_buffer=$BUFFER"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            "command --select hello",
            "command --previous --select",
        ]
        assert "command_buffer=echo generated" in result.stdout
        assert "previous_buffer=echo previous" in result.stdout
        zle_lines = [
            line
            for line in (tmp / "zle.log").read_text(encoding="utf-8").splitlines()
            if not line.startswith("-N:")
        ]
        assert zle_lines == [
            "-I:, hello",
            "reset-prompt:echo generated",
            "-I:,,",
            "reset-prompt:echo previous",
        ]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_accept_line_inserts_fix_proposals_without_executing() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                '                    function zle { :; }\n                    source shell/zsh/sigil.zsh\n                    BUFFER="^"\n                    CURSOR=1\n                    __sigil_accept_line\n                    print -- "buffer=$BUFFER"\n                    print -- "cursor=$CURSOR"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == ["fix"]
        assert "buffer=echo fix" in result.stdout
        assert "cursor=8" in result.stdout


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_fix_function_inserts_instead_of_printing_to_stdout() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                "                    source shell/zsh/sigil.zsh\n                    sigil_fix\n                    print -- done\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == ["fix"]
        assert result.stdout == "done\n"


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_blocks_execute_and_promotion_routes_before_cli() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                '                    function zle { :; }\n                    source shell/zsh/sigil.zsh\n                    BUFFER=",! rm -rf nope"\n                    __sigil_accept_line\n                    print -- "bang_buffer=$BUFFER"\n                    BUFFER="@ promote"\n                    __sigil_accept_line\n                    print -- "at_buffer=$BUFFER"\n                    BUFFER="?! run"\n                    __sigil_accept_line\n                    print -- "question_bang_buffer=$BUFFER"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == []
        assert "bang_buffer=,! rm -rf nope" in result.stdout
        assert "at_buffer=@ promote" in result.stdout
        assert "question_bang_buffer=?! run" in result.stdout


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


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_question_routes_do_not_quote_the_visible_buffer() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                '                    function zle { print -- "$1:$BUFFER" >> "$ZLE_LOG"; }\n                    source shell/zsh/sigil.zsh\n                    BUFFER="? hello"\n                    __sigil_accept_line\n                    BUFFER="?? hello"\n                    __sigil_accept_line\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == ["question hello", "question --follow-up hello"]
        zle_lines = [
            line
            for line in (tmp / "zle.log").read_text(encoding="utf-8").splitlines()
            if not line.startswith("-N:")
        ]
        assert zle_lines == [
            "-I:? hello",
            "reset-prompt:",
            "-I:?? hello",
            "reset-prompt:",
        ]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_summary_route_is_read_only_and_clears_the_prompt_buffer() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                '                    function zle { :; }\n                    source shell/zsh/sigil.zsh\n                    BUFFER="@. now"\n                    __sigil_accept_line\n                    print -- "summary_buffer=$BUFFER"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == ["summary now"]
        assert "summary_buffer=" in result.stdout
