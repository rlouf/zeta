from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from sigil.cli import cli, main
from sigil.install import DoctorCheck, doctor_checks, install_shell


class InstallShellTests(unittest.TestCase):
    def test_install_shell_copies_binding_and_updates_rc_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_dir = root / "bindings"
            rc_path = root / ".bashrc"

            first = install_shell("bash", install_dir=install_dir, rc_path=rc_path)
            second = install_shell("bash", install_dir=install_dir, rc_path=rc_path)

            binding_path = install_dir / "sigil.bash"
            self.assertTrue(binding_path.exists())
            self.assertIn("Sigil bash bindings", binding_path.read_text())
            self.assertTrue(first.wrote_rc)
            self.assertFalse(second.wrote_rc)
            self.assertEqual(rc_path.read_text().count("source "), 1)

    def test_install_shell_cli_json_reports_paths(self) -> None:
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

            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["shell"], "zsh")
            self.assertTrue(Path(payload["binding_path"]).exists())
            self.assertTrue(Path(payload["rc_path"]).exists())
            self.assertTrue(payload["wrote_rc"])

    def test_install_shell_alias_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = CliRunner().invoke(
                cli,
                [
                    "install-shell",
                    "bash",
                    "--install-dir",
                    str(root / "bash"),
                    "--rc",
                    str(root / ".bashrc"),
                    "--json",
                ],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["shell"], "bash")
            self.assertTrue(Path(payload["binding_path"]).exists())


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_expected_checks(self) -> None:
        fake_env = {
            "SHELL": "/bin/bash",
            "SIGIL_SESSION_ID": "test-session",
            "QWEN_MODEL": "qwen-test",
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, fake_env, clear=True):
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
                                    "shell:binding-installed",
                                    "ok",
                                    "/tmp/sigil.bash",
                                ),
                            ):
                                checks = doctor_checks()

        names = {check.name for check in checks}
        self.assertIn("executable:sigil", names)
        self.assertIn("executable:fzf", names)
        self.assertIn("executable:glow", names)
        self.assertIn("executable:pi", names)
        self.assertIn("model:endpoint", names)
        self.assertIn("model:name", names)
        self.assertIn("state:writable", names)
        self.assertIn("shell:supported", names)
        self.assertIn("shell:binding-installed", names)
        self.assertIn("shell:binding-loaded", names)
        self.assertTrue(all(check.status == "ok" for check in checks))

    def test_doctor_cli_json_returns_nonzero_for_failures(self) -> None:
        checks = [
            DoctorCheck("executable:sigil", "ok", "/bin/sigil"),
            DoctorCheck("executable:fzf", "fail", "fzf is not on PATH"),
        ]
        stdout = StringIO()
        with patch("sigil.cli.doctor_checks", return_value=checks):
            with redirect_stdout(stdout):
                code = main(["doctor", "--json"])

        self.assertEqual(code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload[1]["name"], "executable:fzf")
        self.assertEqual(payload[1]["status"], "fail")


if __name__ == "__main__":
    unittest.main()
