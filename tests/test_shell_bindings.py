from __future__ import annotations
import pytest
import os
import shutil
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
            if [ "$*" = "status --json" ]; then
              if [ "${SIGIL_STUB_STATUS:-clean}" = "attention" ]; then
                printf '%s\n' '{"state":"attention"}'
                exit 1
              fi
              printf '%s\n' '{"state":"clean"}'
              exit 0
            fi
            if [ "$*" = "staged pop" ]; then
              [ -n "${SIGIL_STUB_STAGED:-}" ] || exit 1
              printf '%s\n' "$SIGIL_STUB_STAGED"
              exit 0
            fi
            if [ "$*" = "transcript append" ]; then
              cat >/dev/null
              printf '%s\n' '{"id":"evt"}'
              exit 0
            fi
            printf '%s\n' "$*" >> "$SIGIL_STUB_LOG"
            case "$*" in
              "model stream")
                request="$(cat)"
                case "$request" in
                  *repair*) command="uv run pytest"; reason="Run tests." ;;
                  *"run it"*) command="echo piped"; reason="Run piped handoff." ;;
                  *) command="echo zeta"; reason="Run zeta handoff." ;;
                esac
                printf '{"type":"tool_call","name":"bash","input":{"command":%s,"reason":%s}}\n' \
                  "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$command")" \
                  "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$reason")"
                ;;
              "tool bash --analyze")
                cat >/dev/null
                printf '%s\n' '{"valid":true,"resolved":true,"effects":[{"kind":"execute","resource":"process","target":"echo","certainty":"certain"}],"diagnostics":[]}'
                ;;
              "tool bash")
                params="$(cat)"
                command="$(printf '%s\n' "$params" | python3 -c 'import json,sys; print(json.load(sys.stdin)["command"])')"
                reason="$(printf '%s\n' "$params" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("reason","Run command."))')"
                printf '{"ok":true,"handoff":{"type":"shell_prompt","command":%s,"reason":%s}}\n' \
                  "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$command")" \
                  "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$reason")"
                ;;
              "command draft executive summary") printf '%s\n' "stream command" ;;
              "ask hello") printf '%s\n' "answer" ;;
              "op , hello") printf '%s\n%s\n' "echo recommended" "because it is safe" ;;
              "op , draft executive summary") printf '%s\n%s\n' "echo stream recommended" "because stdin matters" ;;
              op*) printf '%s\n' "op:$*" ;;
              run*) printf '%s\n' "ran:${*:2}" ;;
              record-failure*) printf '%s\n' "recorded" ;;
              record-turn*) printf '%s\n' "turn-recorded" ;;
              *) printf '%s\n' "unexpected:$*" >&2; exit 64 ;;
            esac
            """
        ).lstrip(),
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def run_shell(
    shell: str, script: str, tmp: Path, stub: Path
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["SIGIL_BIN"] = str(stub)
    env["ZETA_BIN"] = str(stub)
    env["SIGIL_STUB_LOG"] = str(tmp / "calls.log")
    env["SIGIL_SESSION_ID"] = "shell-test"
    env["ZLE_LOG"] = str(tmp / "zle.log")
    for leaked in ("SIGIL_TTY", "SIGIL_TTY_FD", "TTY"):
        env.pop(leaked, None)
    return subprocess.run(
        [shell, "-c", script],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def run_shell_args(
    args: list[str], script: str, tmp: Path, stub: Path
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["SIGIL_BIN"] = str(stub)
    env["ZETA_BIN"] = str(stub)
    env["SIGIL_STUB_LOG"] = str(tmp / "calls.log")
    env["SIGIL_SESSION_ID"] = "shell-test"
    env["ZLE_LOG"] = str(tmp / "zle.log")
    for leaked in ("SIGIL_TTY", "SIGIL_TTY_FD", "TTY"):
        env.pop(leaked, None)
    return subprocess.run(
        [*args, script],
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


def zeta_bash_turn_calls() -> list[str]:
    return ["model stream", "tool bash --analyze", "tool bash"]


def test_bash_wrappers_call_current_cli_contract() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    source src/sigil/shell/bash/sigil.bash\n                    sigil_command hello\n                    sigil_agent_step hello\n                    sigil_question hello\n                    sigil_web_question hello\n                    printf 'history=%s\\n' \"$(__sigil_history_line)\"\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            "op , hello",
            *zeta_bash_turn_calls(),
            "op ? hello",
            "op ?? hello",
        ]
        assert "echo recommended" in result.stdout
        assert "because it is safe" in result.stdout
        assert "❯ bash   echo zeta" in result.stdout
        assert "Run zeta handoff." in result.stdout
        assert "op:op ?? hello" in result.stdout
        assert "history=echo zeta" in result.stdout


def test_bash_agent_and_goal_wrappers_call_operator_contract() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    source src/sigil/shell/bash/sigil.bash\n                    sigil_agent_step hello\n                    sigil_agent_step_auto hello\n                    sigil_goal hello\n                    sigil_goal_auto hello\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            *zeta_bash_turn_calls(),
            *zeta_bash_turn_calls(),
            "op @ hello",
            "op @@ hello",
        ]


def test_bash_recommendations_print_stdout_and_command_to_history() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    source src/sigil/shell/bash/sigil.bash\n                    sigil_command hello\n                    printf 'history=%s\\n' \"$(__sigil_history_line)\"\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert result.stdout == (
            "echo recommended\nbecause it is safe\nhistory=echo recommended\n"
        )


def test_bash_question_does_not_consume_staged_command() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    source src/sigil/shell/bash/sigil.bash\n                    sigil_command hello\n                    export SIGIL_STUB_STAGED='git diff --stat'\n                    sigil_question review\n                    printf 'history=%s\\n' \"$(__sigil_history_line)\"\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert "op:op ? review" in result.stdout
        assert "history=echo recommended" in result.stdout


def test_bash_agent_step_does_not_consume_staged_command() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    source src/sigil/shell/bash/sigil.bash\n                    export SIGIL_STUB_STAGED='uv run pytest'\n                    sigil_agent_step_auto repair\n                    printf 'history=%s\\n' \"$(__sigil_history_line)\"\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert "Run tests." in result.stdout
        assert "history=uv run pytest" in result.stdout


def test_bash_exports_tty_for_pipeline_confirmations() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    unset SIGIL_TTY\n                    export TTY=/tmp/sigil-test-tty\n                    source src/sigil/shell/bash/sigil.bash\n                    printf 'sigil_tty=%s\\n' \"$SIGIL_TTY\"\n                    "
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
                '                    unset SIGIL_TTY\n                    export TTY=/tmp/sigil-test-tty\n                    source src/sigil/shell/zsh/sigil.zsh\n                    print -- "sigil_tty=$SIGIL_TTY"\n                    '
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
                "                    source src/sigil/shell/bash/sigil.bash\n                    printf 'diff\\n' | sigil_web_question review risky changes\n                    printf 'notes\\n' | sigil_command draft executive summary\n                    printf 'cmd\\n' | sigil_agent_step run it\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            "op ?? review risky changes",
            "op , draft executive summary",
            *zeta_bash_turn_calls(),
        ]
        assert "echo stream recommended" in result.stdout
        assert "because stdin matters" in result.stdout


def test_bash_records_every_non_sigil_turn_via_record_turn() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    source src/sigil/shell/bash/sigil.bash\n                    __sigil_history_entry() { printf '1\\t%s\\n' \"ls -la\"; }\n                    true\n                    __sigil_precmd\n                    __sigil_history_entry() { printf '2\\t%s\\n' \"bad command\"; }\n                    false\n                    __sigil_precmd\n                    __sigil_history_entry() { printf '3\\t%s\\n' \", should not record\"; }\n                    false\n                    __sigil_precmd\n                    wait\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert sorted(read_log(tmp)) == sorted(
            [
                f"record-turn --status 0 --cwd {ROOT} ls -la",
                f"record-turn --status 1 --cwd {ROOT} bad command",
            ]
        )


def test_bash_records_repeated_command_when_history_id_changes() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    source src/sigil/shell/bash/sigil.bash\n                    __sigil_history_entry() { printf '1\\t%s\\n' \"ls\"; }\n                    true\n                    __sigil_precmd\n                    __sigil_history_entry() { printf '2\\t%s\\n' \"ls\"; }\n                    true\n                    __sigil_precmd\n                    wait\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            f"record-turn --status 0 --cwd {ROOT} ls",
            f"record-turn --status 0 --cwd {ROOT} ls",
        ]


def test_bash_dedupes_same_history_entry() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    source src/sigil/shell/bash/sigil.bash\n                    __sigil_history_entry() { printf '1\\t%s\\n' \"ls\"; }\n                    true\n                    __sigil_precmd\n                    true\n                    __sigil_precmd\n                    wait\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [f"record-turn --status 0 --cwd {ROOT} ls"]


def test_bash_does_not_record_sigil_commands() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    source src/sigil/shell/bash/sigil.bash\n                    __sigil_history_entry() { printf '1\\t%s\\n' \"sigil bad\"; }\n                    false\n                    __sigil_precmd\n                    wait\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == []


def test_bash_does_not_record_sigil_wrapper_commands() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    source src/sigil/shell/bash/sigil.bash\n                    __sigil_history_entry() { printf '1\\t%s\\n' \"sigil_command hello\"; }\n                    sigil_command hello\n                    __sigil_precmd\n                    wait\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert result.stdout == "echo recommended\nbecause it is safe\n"
        assert read_log(tmp) == ["op , hello"]


def test_bash_run_glyph_dispatches_to_sigil_run() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    source src/sigil/shell/bash/sigil.bash\n                    + echo captured\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert result.stdout == "ran:echo captured\n"
        assert read_log(tmp) == ["run echo captured"]


def test_bash_passes_failure_snippet_env_to_record_turn() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                '                    source src/sigil/shell/bash/sigil.bash\n                    __sigil_history_entry() { printf \'1\\t%s\\n\' "bad command"; }\n                    export SIGIL_FAILURE_STDOUT="stdout line"\n                    export SIGIL_FAILURE_STDERR="stderr line"\n                    false\n                    __sigil_precmd\n                    wait\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            f"record-turn --status 1 --cwd {ROOT} --stdout-snippet stdout line --stderr-snippet stderr line bad command"
        ]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_wrappers_call_current_cli_contract() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                '                    source src/sigil/shell/zsh/sigil.zsh\n                    sigil_command hello\n                    sigil_agent_step hello\n                    sigil_question hello\n                    sigil_web_question hello\n                    print -- "history=${history[$HISTCMD]}"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            "op , hello",
            *zeta_bash_turn_calls(),
            "op ? hello",
            "op ?? hello",
        ]
        assert "echo recommended" in result.stdout
        assert "because it is safe" in result.stdout
        assert "❯ bash   echo zeta" in result.stdout
        assert "Run zeta handoff." in result.stdout
        assert "op:op ?? hello" in result.stdout
        assert "history=echo zeta" in result.stdout


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_question_does_not_consume_staged_command() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                '                    source src/sigil/shell/zsh/sigil.zsh\n                    sigil_command hello\n                    export SIGIL_STUB_STAGED="git diff --stat"\n                    sigil_question review\n                    print -- "history=${history[$HISTCMD]}"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert "op:op ? review" in result.stdout
        assert "history=echo recommended" in result.stdout


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_agent_step_does_not_consume_staged_command() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                '                    source src/sigil/shell/zsh/sigil.zsh\n                    export SIGIL_STUB_STAGED="uv run pytest"\n                    sigil_agent_step_auto repair\n                    print -- "history=${history[$HISTCMD]}"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert "Run tests." in result.stdout
        assert "history=uv run pytest" in result.stdout


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_agent_and_goal_wrappers_call_operator_contract() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                "                    source src/sigil/shell/zsh/sigil.zsh\n                    sigil_agent_step hello\n                    sigil_agent_step_auto hello\n                    sigil_goal hello\n                    sigil_goal_auto hello\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            *zeta_bash_turn_calls(),
            *zeta_bash_turn_calls(),
            "op @ hello",
            "op @@ hello",
        ]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_wrappers_dispatch_piped_stdin_to_operator_runtime() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                "                    source src/sigil/shell/zsh/sigil.zsh\n                    printf 'diff\\n' | sigil_web_question review risky changes\n                    printf 'notes\\n' | sigil_command draft executive summary\n                    printf 'cmd\\n' | sigil_agent_step run it\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            "op ?? review risky changes",
            "op , draft executive summary",
            *zeta_bash_turn_calls(),
        ]
        assert "op:op ?? review risky changes" in result.stdout
        assert "echo stream recommended" in result.stdout
        assert "because stdin matters" in result.stdout
        assert "Run piped handoff." in result.stdout


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_glyph_aliases_dispatch_piped_stdin_before_globbing() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                "                    source src/sigil/shell/zsh/sigil.zsh\n                    eval \"printf 'diff\\\\n' | ?? review risky changes\"\n                    eval \"printf 'notes\\\\n' | , draft executive summary\"\n                    eval \"printf 'cmd\\\\n' | ,, run it\"\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            "op ?? review risky changes",
            "op , draft executive summary",
            *zeta_bash_turn_calls(),
        ]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_does_not_record_sigil_commands() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                '                    source src/sigil/shell/zsh/sigil.zsh\n                    __sigil_before_command "sigil bad"\n                    false\n                    __sigil_after_command_before_prompt\n                    wait\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == []


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_does_not_record_sigil_wrapper_commands() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                '                    source src/sigil/shell/zsh/sigil.zsh\n                    __sigil_before_command "noglob sigil_command hello"\n                    sigil_command hello\n                    __sigil_after_command_before_prompt\n                    wait\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert result.stdout == "echo recommended\nbecause it is safe\n"
        assert read_log(tmp) == ["op , hello"]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_run_glyph_dispatches_to_sigil_run() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                "                    source src/sigil/shell/zsh/sigil.zsh\n                    + echo captured\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert result.stdout == "ran:echo captured\n"
        assert read_log(tmp) == ["run echo captured"]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_records_every_non_sigil_turn_via_record_turn() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                '                    source src/sigil/shell/zsh/sigil.zsh\n                    __sigil_before_command "ls -la"\n                    true\n                    __sigil_after_command_before_prompt\n                    __sigil_before_command "bad command"\n                    false\n                    __sigil_after_command_before_prompt\n                    __sigil_before_command ", should not record"\n                    false\n                    __sigil_after_command_before_prompt\n                    wait\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert sorted(read_log(tmp)) == sorted(
            [
                f"record-turn --status 0 --cwd {ROOT} ls -la",
                f"record-turn --status 1 --cwd {ROOT} bad command",
            ]
        )


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_history_filter_is_additive_and_covers_glyphs() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell_args(
            ["zsh", "-f", "-ic"],
            textwrap.dedent(
                '                    function zshaddhistory() { print -- "user:$1" >> "$ZLE_LOG"; return 0; }\n                    source src/sigil/shell/zsh/sigil.zsh\n                    print -- "hooks=$zshaddhistory_functions"\n                    zshaddhistory "echo hello"\n                    __sigil_zshaddhistory ", hello"; print -- "comma=$?"\n                    __sigil_zshaddhistory "? hello"; print -- "question=$?"\n                    __sigil_zshaddhistory "\\? hello"; print -- "escaped_question=$?"\n                    __sigil_zshaddhistory "+ echo"; print -- "run=$?"\n                    __sigil_zshaddhistory "@ hello"; print -- "at=$?"\n                    __sigil_zshaddhistory "echo hello"; print -- "echo=$?"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert "__sigil_zshaddhistory" in result.stdout
        assert "comma=1" in result.stdout
        assert "question=1" in result.stdout
        assert "escaped_question=1" in result.stdout
        assert "run=1" in result.stdout
        assert "at=1" in result.stdout
        assert "echo=0" in result.stdout
        assert (tmp / "zle.log").read_text(encoding="utf-8") == "user:echo hello\n"
