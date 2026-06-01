from __future__ import annotations
import json
import os
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from click.testing import CliRunner

from _patch import patch, patch_dict
from sigil.cli import cli, main
from sigil.install import DoctorCheck, doctor_checks, install_shell


def test_install_shell_copies_binding_and_updates_rc_idempotently() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        install_dir = root / "bindings"
        rc_path = root / ".bashrc"
        first = install_shell("bash", install_dir=install_dir, rc_path=rc_path)
        second = install_shell("bash", install_dir=install_dir, rc_path=rc_path)
        binding_path = install_dir / "sigil.bash"
        assert binding_path.exists()
        assert "Sigil bash bindings" in binding_path.read_text()
        assert first.wrote_rc
        assert not second.wrote_rc
        assert rc_path.read_text().count("source ") == 1
        assert "export SIGIL_ENABLE_GLYPHS=1" in rc_path.read_text()


def test_install_shell_can_disable_glyph_aliases_in_rc_snippet() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        result = install_shell(
            "zsh",
            install_dir=root / "bindings",
            rc_path=root / ".zshrc",
            enable_glyphs=False,
        )
        rc_text = (root / ".zshrc").read_text(encoding="utf-8")
        assert not result.glyphs_enabled
        assert "export SIGIL_ENABLE_GLYPHS=0" in rc_text


def test_install_shell_bakes_resolved_sigil_and_zeta_bins_into_rc() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bins = {"sigil": "/opt/sigil/bin/sigil", "zeta": "/opt/sigil/bin/zeta"}
        with patch("sigil.install.shutil.which", side_effect=bins.get):
            install_shell(
                "zsh",
                install_dir=root / "bindings",
                rc_path=root / ".zshrc",
            )

        rc_text = (root / ".zshrc").read_text(encoding="utf-8")
        assert "export SIGIL_BIN=/opt/sigil/bin/sigil" in rc_text
        assert "export ZETA_BIN=/opt/sigil/bin/zeta" in rc_text


def test_install_shell_cli_json_reports_paths() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        result = CliRunner().invoke(
            cli,
            [
                "install",
                "zsh",
                "--install-dir",
                str(root / "zsh"),
                "--rc",
                str(root / ".zshrc"),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["shell"] == "zsh"
        assert Path(payload["binding_path"]).exists()
        assert Path(payload["rc_path"]).exists()
        assert payload["wrote_rc"]
        assert payload["glyphs_enabled"]


def test_doctor_reports_expected_checks() -> None:
    fake_env = {
        "SHELL": "/bin/bash",
        "SIGIL_SESSION_ID": "test-session",
        "SIGIL_MODEL_NAME": "model-test",
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
                                "shell:binding-installed", "ok", "/tmp/sigil.bash"
                            ),
                        ):
                            checks = doctor_checks()
    names = {check.name for check in checks}
    assert "executable:sigil" in names
    assert "executable:glow" in names
    assert "executable:zeta" in names
    assert "model:endpoint" in names
    assert "model:name" in names
    assert "state:writable" in names
    assert "shell:supported" in names
    assert "shell:binding-installed" in names
    assert "shell:binding-loaded" in names
    assert all((check.status == "ok" for check in checks))


def test_doctor_cli_json_returns_nonzero_for_failures() -> None:
    checks = [
        DoctorCheck("executable:sigil", "ok", "/bin/sigil"),
        DoctorCheck("executable:zeta", "fail", "zeta is not on PATH"),
    ]
    stdout = StringIO()
    with patch("sigil.cli.install.doctor_checks", return_value=checks):
        with redirect_stdout(stdout):
            code = main(["doctor", "--json"])
    assert code == 1
    payload = json.loads(stdout.getvalue())
    assert payload[1]["name"] == "executable:zeta"
    assert payload[1]["status"] == "fail"
