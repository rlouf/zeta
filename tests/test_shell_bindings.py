from __future__ import annotations
import json
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
            if [ "$*" = "transcript append" ]; then
              cat >/dev/null
              printf '%s\n' '{"id":"evt"}'
              exit 0
            fi
            if [ "$*" = "transcript shell-result" ]; then
              printf '%s\n' "$*" >> "$SIGIL_STUB_LOG"
              printf '%s\n' '{"id":"shell-result"}'
              exit 0
            fi
            if [ "$*" = "transcript shell-turn" ]; then
              payload="$(cat)"
              printf '%s\t%s\n' "$*" "$payload" >> "$SIGIL_STUB_LOG"
              printf '%s\n' '{"ok":true}'
              exit 0
            fi
            if [ "$1" = "zeta-step" ]; then
              continue_step=0
              handoff_file=""
              objective=""
              while [ "$#" -gt 0 ]; do
                case "$1" in
                  --handoff-file)
                    handoff_file="$2"
                    shift 2
                    ;;
                  --glyph)
                    shift 2
                    ;;
                  --continue)
                    continue_step=1
                    shift
                    ;;
                  zeta-step)
                    shift
                    ;;
                  *)
                    objective="$1"
                    shift
                    ;;
                esac
              done
              if [ "$continue_step" = "1" ]; then
                printf '%s\n' "zeta-step --continue" >> "$SIGIL_STUB_LOG"
                command="echo continued"
                reason="Continue after shell handoff."
              else
                printf '%s\n' "zeta-step" >> "$SIGIL_STUB_LOG"
                case "$objective" in
                  *repair*) command="uv run pytest"; reason="Run tests." ;;
                  *"run it"*) command="echo piped"; reason="Run piped handoff." ;;
                  *) command="echo zeta"; reason="Run zeta handoff." ;;
                esac
              fi
              printf '❯ bash   %s\n' "$command"
              printf '  staged in prompt\n'
              if [ -n "$handoff_file" ]; then
                printf '{"type":"shell_prompt","command":%s,"reason":%s}\n' \
                  "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$command")" \
                  "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$reason")" \
                  > "$handoff_file"
              fi
              exit 0
            fi
            if [ "$1" = "display" ]; then
              payload="$(cat)"
              case "$*" in
                "display tool-result bash") printf '%s\n' "staged in prompt" ;;
                "display tool-result read") printf '%s\n' "2 lines · 4 bytes" ;;
                "display tool-result ls") printf '%s\n' "2 entries" ;;
                "display tool-result grep") printf '%s\n' "2 matches · 2 files" ;;
                "display shell-result")
                  case "$payload" in
                    *'"outcome":"executed"'*) printf '%s\n%s\n%s\n' "❯ shell  captured" "  uv run pytest" "  exit 0 · 1 shell turn" ;;
                    *'"outcome":"cancelled"'*) printf '%s\n%s\n%s\n' "❯ shell  changed" "  expected: uv run pytest" "  ran:      uv run pytest -q" ;;
                  esac
                  ;;
              esac
              exit 0
            fi
            printf '%s\n' "$*" >> "$SIGIL_STUB_LOG"
            case "$*" in
              "command draft executive summary") printf '%s\n' "stream command" ;;
              "ask hello") printf '%s\n' "answer" ;;
              "ask draft executive summary") printf '%s\n' "readonly stream answer" ;;
              run*) printf '%s\n' "ran:${*:2}" ;;
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


def zeta_step_calls() -> list[str]:
    return ["zeta-step"]


def shell_turn_payloads(tmp: Path) -> list[dict[str, object]]:
    payloads = []
    for line in read_log(tmp):
        if not line.startswith("transcript shell-turn\t"):
            continue
        payloads.append(json.loads(line.split("\t", 1)[1]))
    return payloads


def test_bash_wrappers_call_current_cli_contract() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    source src/sigil/shell/bash/sigil.bash\n                    sigil_command hello\n                    sigil_agent_step hello\n                    printf 'history=%s\\n' \"$(__sigil_history_line)\"\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            "ask hello",
            *zeta_step_calls(),
        ]
        assert "answer" in result.stdout
        assert "❯ bash   echo zeta" in result.stdout
        assert "  staged in prompt" in result.stdout
        assert "Run zeta handoff." not in result.stdout
        assert "history=echo zeta" in result.stdout


def test_bash_agent_wrappers_call_zeta_loop() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    source src/sigil/shell/bash/sigil.bash\n                    sigil_agent_step hello\n                    sigil_agent_step_auto hello\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            *zeta_step_calls(),
            *zeta_step_calls(),
        ]


def test_bash_comma_prints_readonly_answer_without_history_insert() -> None:
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
        assert result.stdout == "answer\nhistory=\n"


def test_bash_agent_step_uses_zeta_handoff_directly() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    source src/sigil/shell/bash/sigil.bash\n                    sigil_agent_step_auto repair\n                    printf 'history=%s\\n' \"$(__sigil_history_line)\"\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert "  staged in prompt" in result.stdout
        assert "Run tests." not in result.stdout
        assert "history=uv run pytest" in result.stdout


def test_bash_bare_agent_step_continues_after_shell_handoff() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    source src/sigil/shell/bash/sigil.bash\n                    sigil_agent_step\n                    printf 'history=%s\\n' \"$(__sigil_history_line)\"\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == ["zeta-step --continue"]
        assert "  staged in prompt" in result.stdout
        assert "Continue after shell handoff." not in result.stdout
        assert "history=echo continued" in result.stdout


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
                "                    source src/sigil/shell/bash/sigil.bash\n                    printf 'notes\\n' | sigil_command draft executive summary\n                    printf 'cmd\\n' | sigil_agent_step run it\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            "ask draft executive summary",
            *zeta_step_calls(),
        ]
        assert "readonly stream answer" in result.stdout


def test_bash_does_not_record_ordinary_turns_ambiently() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    source src/sigil/shell/bash/sigil.bash\n                    true\n                    false\n                    wait\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == []


def test_bash_records_turns_only_after_zeta_handoff() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    source src/sigil/shell/bash/sigil.bash\n                    sigil_agent_step hello >/dev/null\n                    __sigil_history_entry() { printf '1\\t%s\\n' \"echo edited\"; }\n                    true\n                    __sigil_zeta_prompt_capture\n                    wait\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        payloads = shell_turn_payloads(tmp)
        assert len(payloads) == 1
        assert payloads[0]["command"] == "echo edited"
        assert payloads[0]["status"] == 0
        assert payloads[0]["cwd"] == str(ROOT)


def test_bash_does_not_install_prompt_recording_hook() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    source src/sigil/shell/bash/sigil.bash\n                    declare -p PROMPT_COMMAND 2>/dev/null || true\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert "__sigil_precmd" not in result.stdout
        assert read_log(tmp) == []


def test_bash_missing_ambient_hook_cannot_record_history() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    source src/sigil/shell/bash/sigil.bash\n                    type __sigil_precmd >/dev/null 2>&1; printf 'has_precmd=%s\\n' \"$?\"\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert result.stdout == "has_precmd=1\n"
        assert read_log(tmp) == []


def test_bash_does_not_record_sigil_commands() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                "                    source src/sigil/shell/bash/sigil.bash\n                    false\n                    wait\n                    "
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
                "                    source src/sigil/shell/bash/sigil.bash\n                    sigil_command hello\n                    wait\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert result.stdout == "answer\n"
        assert read_log(tmp) == ["ask hello"]


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


def test_bash_failure_snippet_env_is_not_ambiently_recorded() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                '                    source src/sigil/shell/bash/sigil.bash\n                    export SIGIL_FAILURE_STDOUT="stdout line"\n                    export SIGIL_FAILURE_STDERR="stderr line"\n                    false\n                    wait\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == []


def test_bash_passes_failure_snippets_during_zeta_handoff_capture() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "bash",
            textwrap.dedent(
                '                    source src/sigil/shell/bash/sigil.bash\n                    sigil_agent_step hello >/dev/null\n                    __sigil_history_entry() { printf \'1\\t%s\\n\' "bad command"; }\n                    export SIGIL_FAILURE_STDOUT="stdout line"\n                    export SIGIL_FAILURE_STDERR="stderr line"\n                    false\n                    __sigil_zeta_prompt_capture || true\n                    wait\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        payloads = shell_turn_payloads(tmp)
        assert len(payloads) == 1
        assert payloads[0]["command"] == "bad command"
        assert payloads[0]["status"] == 1
        assert payloads[0]["stdout_snippet"] == "stdout line"
        assert payloads[0]["stderr_snippet"] == "stderr line"


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_wrappers_call_current_cli_contract() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                '                    source src/sigil/shell/zsh/sigil.zsh\n                    sigil_command hello\n                    sigil_agent_step hello\n                    print -- "history=${history[$HISTCMD]}"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            "ask hello",
            *zeta_step_calls(),
        ]
        assert "answer" in result.stdout
        assert "❯ bash   echo zeta" in result.stdout
        assert "  staged in prompt" in result.stdout
        assert "Run zeta handoff." not in result.stdout
        assert "history=echo zeta" in result.stdout


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_agent_step_uses_zeta_handoff_directly() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                '                    source src/sigil/shell/zsh/sigil.zsh\n                    sigil_agent_step_auto repair\n                    print -- "history=${history[$HISTCMD]}"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert "  staged in prompt" in result.stdout
        assert "Run tests." not in result.stdout
        assert "history=uv run pytest" in result.stdout


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_bare_agent_step_continues_after_shell_handoff() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                '                    source src/sigil/shell/zsh/sigil.zsh\n                    sigil_agent_step\n                    print -- "history=${history[$HISTCMD]}"\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == ["zeta-step --continue"]
        assert "  staged in prompt" in result.stdout
        assert "Continue after shell handoff." not in result.stdout
        assert "history=echo continued" in result.stdout


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_agent_wrappers_call_zeta_loop() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                "                    source src/sigil/shell/zsh/sigil.zsh\n                    sigil_agent_step hello\n                    sigil_agent_step_auto hello\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            *zeta_step_calls(),
            *zeta_step_calls(),
        ]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_wrappers_dispatch_piped_stdin_to_operator_runtime() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                "                    source src/sigil/shell/zsh/sigil.zsh\n                    printf 'notes\\n' | sigil_command draft executive summary\n                    printf 'cmd\\n' | sigil_agent_step run it\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            "ask draft executive summary",
            *zeta_step_calls(),
        ]
        assert "readonly stream answer" in result.stdout
        assert "  staged in prompt" in result.stdout
        assert "Run piped handoff." not in result.stdout


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_glyph_aliases_dispatch_piped_stdin_before_globbing() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                "                    source src/sigil/shell/zsh/sigil.zsh\n                    eval \"printf 'notes\\\\n' | , draft executive summary\"\n                    eval \"printf 'cmd\\\\n' | ,, run it\"\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == [
            "ask draft executive summary",
            *zeta_step_calls(),
        ]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_does_not_record_sigil_commands() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                "                    source src/sigil/shell/zsh/sigil.zsh\n                    false\n                    wait\n                    "
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
                "                    source src/sigil/shell/zsh/sigil.zsh\n                    sigil_command hello\n                    wait\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert result.stdout == "answer\n"
        assert read_log(tmp) == ["ask hello"]


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
def test_zsh_does_not_record_ordinary_turns_ambiently() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                "                    source src/sigil/shell/zsh/sigil.zsh\n                    true\n                    false\n                    wait\n                    "
            ),
            tmp,
            stub,
        )
        assert_success(result)
        assert read_log(tmp) == []


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_zsh_records_turns_only_after_zeta_handoff() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        stub = make_stub(tmp)
        result = run_shell(
            "zsh",
            textwrap.dedent(
                '                    source src/sigil/shell/zsh/sigil.zsh\n                    sigil_agent_step hello >/dev/null\n                    __sigil_zeta_before_command "echo edited"\n                    true\n                    __sigil_zeta_after_command_before_prompt\n                    wait\n                    '
            ),
            tmp,
            stub,
        )
        assert_success(result)
        payloads = shell_turn_payloads(tmp)
        assert len(payloads) == 1
        assert payloads[0]["command"] == "echo edited"
        assert payloads[0]["status"] == 0
        assert payloads[0]["cwd"] == str(ROOT)


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
        assert "question=0" in result.stdout
        assert "escaped_question=0" in result.stdout
        assert "run=1" in result.stdout
        assert "at=0" in result.stdout
        assert "echo=0" in result.stdout
        assert (tmp / "zle.log").read_text(encoding="utf-8") == "user:echo hello\n"
