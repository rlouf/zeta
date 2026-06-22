import json
import os
import socket
import tempfile
import threading
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
from pathlib import Path

from _patch import patch, patch_dict
from _zeta_helpers import write_codex_auth_file
from click.testing import CliRunner

from sigil.cli import cli, main
from sigil.cli._base import EXIT_ERROR, EXIT_OK
from sigil.install import (
    DoctorCheck,
    check_codex_auth,
    check_endpoint,
    check_session_tty,
    doctor_checks,
    install_zsh_binding,
)


def test_install_zsh_binding_copies_binding_and_updates_rc_idempotently() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        install_dir = root / "bindings"
        rc_path = root / ".zshrc"
        first = install_zsh_binding(install_dir=install_dir, rc_path=rc_path)
        second = install_zsh_binding(install_dir=install_dir, rc_path=rc_path)
        binding_path = install_dir / "sigil.zsh"
        assert binding_path.exists()
        assert "Sigil zsh bindings" in binding_path.read_text()
        assert 'SIGIL_BINDING_LOADED="zsh"' in binding_path.read_text()
        assert first.wrote_rc
        assert not second.wrote_rc
        assert rc_path.read_text().count("source ") == 1
        assert "export SIGIL_ENABLE_GLYPHS=1" in rc_path.read_text()


def test_install_zsh_binding_can_disable_glyph_aliases_in_rc_snippet() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        result = install_zsh_binding(
            install_dir=root / "bindings",
            rc_path=root / ".zshrc",
            enable_glyphs=False,
        )
        rc_text = (root / ".zshrc").read_text(encoding="utf-8")
        assert not result.glyphs_enabled
        assert "export SIGIL_ENABLE_GLYPHS=0" in rc_text


def test_install_zsh_binding_bakes_resolved_runtime_bins_into_rc() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bins = {"sigil": "/opt/sigil/bin/sigil"}
        with patch("sigil.install.shutil.which", side_effect=bins.get):
            install_zsh_binding(
                install_dir=root / "bindings",
                rc_path=root / ".zshrc",
            )

        rc_text = (root / ".zshrc").read_text(encoding="utf-8")
        assert "export SIGIL_BIN=/opt/sigil/bin/sigil" in rc_text
        assert "ZETA_BIN" not in rc_text


def test_install_zsh_binding_cli_json_reports_paths() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        result = CliRunner().invoke(
            cli,
            [
                "install",
                "--install-dir",
                str(root / "zsh"),
                "--rc",
                str(root / ".zshrc"),
                "--json",
            ],
        )
        assert result.exit_code == EXIT_OK, result.output
        payload = json.loads(result.output)
        assert Path(payload["binding_path"]).exists()
        assert Path(payload["rc_path"]).exists()
        assert payload["wrote_rc"]
        assert payload["glyphs_enabled"]


def test_doctor_reports_expected_checks() -> None:
    fake_env = {
        "SHELL": "/bin/zsh",
        "SIGIL_SESSION_ID": "test-session",
        "ZETA_MODEL_NAME": "model-test",
    }
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(os.environ, fake_env, clear=True):
            with patch("sigil.install.shutil.which", return_value="/bin/tool"):
                with patch(
                    "sigil.install.check_state_writable",
                    return_value=DoctorCheck("state:writable", "ok", tmp),
                ):
                    with patch(
                        "sigil.install.check_endpoint",
                        return_value=DoctorCheck(
                            "model:endpoint", "ok", "http://127.0.0.1:8080"
                        ),
                    ):
                        with patch(
                            "sigil.install.check_shell_binding_installed",
                            return_value=DoctorCheck(
                                "shell:binding-installed", "ok", "/tmp/sigil.zsh"
                            ),
                        ):
                            checks = doctor_checks()
    names = {check.name for check in checks}
    assert "sigil:installed" in names
    assert "zeta:installed" not in names
    assert "model:endpoint" in names
    assert "state:writable" in names
    assert "shell:supported" in names
    assert "shell:binding-installed" in names
    assert "shell:binding-loaded" in names
    assert "shell:glyphs-enabled" in names
    assert "shell:session-tty" in names
    assert all(check.status == "ok" for check in checks)


def test_check_session_tty_warns_when_session_came_from_another_tty() -> None:
    env = {
        "SIGIL_SESSION_ID": "pane-a-id",
        "SIGIL_SESSION_TTY": "/dev/ttyFAKE0",
    }
    with patch("sigil.install.current_tty", return_value="/dev/ttys001"):
        check = check_session_tty(env)
    assert check.status == "warn"
    assert "pane-a-id" in check.detail
    assert "/dev/ttyFAKE0" in check.detail
    assert "/dev/ttys001" in check.detail


def test_check_session_tty_ok_on_matching_tty() -> None:
    env = {
        "SIGIL_SESSION_ID": "pane-a-id",
        "SIGIL_SESSION_TTY": "/dev/ttys001",
    }
    with patch("sigil.install.current_tty", return_value="/dev/ttys001"):
        check = check_session_tty(env)
    assert check.status == "ok"


def test_check_session_tty_ok_without_recorded_tty() -> None:
    check = check_session_tty({"SIGIL_SESSION_ID": "manual-id"})
    assert check.status == "ok"


def test_check_session_tty_ok_without_controlling_terminal() -> None:
    env = {
        "SIGIL_SESSION_ID": "pane-a-id",
        "SIGIL_SESSION_TTY": "/dev/ttys001",
    }
    with patch("sigil.install.current_tty", return_value=None):
        check = check_session_tty(env)
    assert check.status == "ok"


def test_doctor_reports_disabled_glyphs() -> None:
    fake_env = {
        "SHELL": "/bin/zsh",
        "SIGIL_SESSION_ID": "test-session",
        "SIGIL_ENABLE_GLYPHS": "0",
    }
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(os.environ, fake_env, clear=True):
            with patch("sigil.install.state_dir", return_value=Path(tmp)):
                with patch("sigil.install.shutil.which", return_value="/bin/tool"):
                    with patch(
                        "sigil.install.check_endpoint",
                        return_value=DoctorCheck(
                            "model:endpoint", "ok", "http://127.0.0.1:8080"
                        ),
                    ):
                        with patch(
                            "sigil.install.check_shell_binding_installed",
                            return_value=DoctorCheck(
                                "shell:binding-installed", "ok", "/tmp/sigil.zsh"
                            ),
                        ):
                            checks = doctor_checks()
    glyphs = next(check for check in checks if check.name == "shell:glyphs-enabled")
    assert glyphs.status == "warn"
    assert glyphs.detail == "glyphs disabled"


def test_doctor_cli_answers_setup_questions() -> None:
    checks = [
        DoctorCheck("sigil:installed", "ok", "/bin/sigil"),
        DoctorCheck("model:endpoint", "warn", "not reachable"),
        DoctorCheck("shell:binding-installed", "ok", "/tmp/sigil.zsh"),
        DoctorCheck("shell:binding-loaded", "ok", "session test"),
        DoctorCheck("shell:glyphs-enabled", "ok", "glyphs enabled"),
    ]
    stdout = StringIO()
    with patch("sigil.cli.install.doctor_checks", return_value=checks):
        with redirect_stdout(stdout):
            code = main(["doctor"])
    output = stdout.getvalue()
    assert code == EXIT_OK
    assert "sigil installed?" in output
    assert "model endpoint reachable?" in output
    assert "shell binding installed?" in output
    assert "shell binding loaded in this shell?" in output
    assert "glyphs enabled?" in output


def test_doctor_cli_json_returns_nonzero_for_failures() -> None:
    checks = [
        DoctorCheck("sigil:installed", "ok", "/bin/sigil"),
        DoctorCheck("model:endpoint", "fail", "not reachable"),
    ]
    stdout = StringIO()
    with patch("sigil.cli.install.doctor_checks", return_value=checks):
        with redirect_stdout(stdout):
            code = main(["doctor", "--json"])
    assert code == EXIT_ERROR
    payload = json.loads(stdout.getvalue())
    assert payload[1]["name"] == "model:endpoint"
    assert payload[1]["status"] == "fail"


def serve_one_non_openai_response() -> tuple[socket.socket, int]:
    server = socket.create_server(("127.0.0.1", 0))
    port = server.getsockname()[1]

    def accept_once() -> None:
        try:
            connection, _ = server.accept()
        except OSError:
            return
        with connection:
            connection.recv(4096)
            connection.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 12\r\n"
                b"Connection: close\r\n"
                b"\r\n"
                b"hello world!"
            )

    threading.Thread(target=accept_once, daemon=True).start()
    return server, port


class ModelsEndpointHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/v1/models":
            self.send_error(404)
            return
        body = json.dumps({"data": [{"id": "local-model"}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def test_doctor_endpoint_warns_when_listener_is_not_openai_compatible() -> None:
    server, port = serve_one_non_openai_response()
    try:
        check = check_endpoint(f"http://127.0.0.1:{port}/v1/chat/completions")
    finally:
        server.close()

    assert check.status == "warn"
    assert "/v1/models" in check.detail


def test_doctor_endpoint_ok_when_models_endpoint_answers() -> None:
    server = HTTPServer(("127.0.0.1", 0), ModelsEndpointHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        check = check_endpoint(f"http://127.0.0.1:{port}/v1/chat/completions")
    finally:
        server.shutdown()
        server.server_close()

    assert check.status == "ok"


def test_doctor_codex_auth_check_absent_without_codex_profiles(tmp_path) -> None:
    with patch_dict(os.environ, {"HOME": str(tmp_path)}):
        assert check_codex_auth() is None


def test_doctor_codex_auth_fails_without_credentials(tmp_path) -> None:
    models = tmp_path / ".zeta" / "models.toml"
    models.parent.mkdir(parents=True)
    models.write_text(
        '[[models]]\nname = "codex"\nmodel = "gpt-5.5"\napi = "codex-responses"\n',
        encoding="utf-8",
    )
    with patch_dict(os.environ, {"HOME": str(tmp_path)}):
        check = check_codex_auth()

    assert check is not None
    assert check.status == "fail"
    assert "codex login" in (check.hint or "")


def test_doctor_codex_auth_ok_with_fresh_credentials(tmp_path) -> None:
    models = tmp_path / ".zeta" / "models.toml"
    models.parent.mkdir(parents=True)
    models.write_text(
        '[[models]]\nname = "codex"\nmodel = "gpt-5.5"\napi = "codex-responses"\n',
        encoding="utf-8",
    )
    write_codex_auth_file(tmp_path / ".codex" / "auth.json")
    with patch_dict(os.environ, {"HOME": str(tmp_path)}):
        check = check_codex_auth()

    assert check is not None
    assert check.status == "ok"


def test_doctor_codex_auth_warns_when_token_expired(tmp_path) -> None:
    models = tmp_path / ".zeta" / "models.toml"
    models.parent.mkdir(parents=True)
    models.write_text(
        '[[models]]\nname = "codex"\nmodel = "gpt-5.5"\napi = "codex-responses"\n',
        encoding="utf-8",
    )
    write_codex_auth_file(tmp_path / ".codex" / "auth.json", expires_in=-60.0)
    with patch_dict(os.environ, {"HOME": str(tmp_path)}):
        check = check_codex_auth()

    assert check is not None
    assert check.status == "warn"
    assert "refresh" in (check.detail or "") + (check.hint or "")


def test_doctor_endpoint_defers_to_codex_auth_for_codex_default(tmp_path) -> None:
    models = tmp_path / ".zeta" / "models.toml"
    models.parent.mkdir(parents=True)
    models.write_text(
        '[[models]]\nname = "codex"\nmodel = "gpt-5.5"\n'
        'api = "codex-responses"\ndefault = true\n',
        encoding="utf-8",
    )
    with patch_dict(os.environ, {"HOME": str(tmp_path), "SIGIL_SESSION_ID": "t"}):
        check = check_endpoint()

    assert check.status == "ok"
    assert "codex-auth" in check.detail
