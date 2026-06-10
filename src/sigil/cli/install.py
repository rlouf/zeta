"""The `install` and `doctor` commands: shell binding setup and health checks."""

from __future__ import annotations

from pathlib import Path

import click

from ..install import (
    DoctorCheck,
    checks_exit_code,
    checks_summary,
    checks_to_json,
    doctor_checks,
    install_zsh_binding,
)
from ._base import cli
from ._shared import pretty_print_json


@cli.command("install")
@click.option(
    "--install-dir",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    help="Directory where the zsh binding should be installed.",
)
@click.option(
    "--rc",
    "rc_path",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Shell rc file to update.",
)
@click.option(
    "--glyphs/--no-glyphs",
    "enable_glyphs",
    default=True,
    show_default=True,
    help="Enable punctuation aliases in the shell rc snippet.",
)
@click.option(
    "--json", "json_output", is_flag=True, help="Emit the install result as JSON."
)
def cmd_install_zsh_binding(
    install_dir: Path | None,
    rc_path: Path | None,
    enable_glyphs: bool,
    json_output: bool,
) -> int:
    """Install or update the Sigil zsh binding."""
    result = install_zsh_binding(
        install_dir=install_dir,
        rc_path=rc_path,
        enable_glyphs=enable_glyphs,
    )
    if json_output:
        pretty_print_json(
            {
                "binding_path": result.binding_path,
                "rc_path": result.rc_path,
                "source_path": result.source_path,
                "wrote_rc": result.wrote_rc,
                "glyphs_enabled": result.glyphs_enabled,
            }
        )
        return 0

    print(f"installed Sigil zsh binding at {result.binding_path}")
    if result.wrote_rc:
        print(f"updated {result.rc_path}")
    else:
        print(f"{result.rc_path} already sources Sigil")
    print(f"restart your shell or run: source {result.rc_path}")
    return 0


@cli.command("doctor")
@click.option("--json", "json_output", is_flag=True, help="Emit doctor checks as JSON.")
def cmd_doctor(json_output: bool) -> int:
    """Check whether Sigil is installed and ready to use."""
    checks = doctor_checks()
    if json_output:
        print(checks_to_json(checks))
        return checks_exit_code(checks)

    for check in checks:
        line = f"{check.status:4} {doctor_label(check)} - {check.detail}"
        print(line)
        if check.hint and check.status != "ok":
            print(f"     hint: {check.hint}")
    summary = checks_summary(checks)
    print(f"{summary['ok']} ok, {summary['warn']} warnings, {summary['fail']} failures")
    return checks_exit_code(checks)


DOCTOR_LABELS = {
    "sigil:installed": "sigil installed?",
    "model:endpoint": "model endpoint reachable?",
    "shell:binding-installed": "shell binding installed?",
    "shell:binding-loaded": "shell binding loaded in this shell?",
    "shell:glyphs-enabled": "glyphs enabled?",
    "shell:supported": "shell supported?",
    "state:writable": "state directory writable?",
}


def doctor_label(check: DoctorCheck) -> str:
    """Return the user-facing label for a doctor check."""
    return DOCTOR_LABELS.get(check.name, check.name)
