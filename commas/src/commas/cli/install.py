"""The `install` and `doctor` commands: shell binding setup and health checks."""

from pathlib import Path

import click

from commas.cli._base import cli, examples
from commas.cli._shared import pretty_print_json
from commas.install import (
    DoctorCheck,
    checks_exit_code,
    checks_summary,
    checks_to_json,
    doctor_checks,
    install_zsh_binding,
)


@cli.command(
    "install",
    epilog=examples(
        "commas install",
        "commas install --no-glyphs",
    ),
)
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
    """Install or update the Commas zsh binding.

    Copies the bundled zsh binding to ~/.zeta/shell/zsh/ and adds an
    idempotent source block to .zshrc. Running it again updates the
    binding without duplicating the rc block.

    With --no-glyphs the rc block loads the named commands but not the
    punctuation aliases.
    """
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

    print(f"installed Commas zsh binding at {result.binding_path}")
    if result.wrote_rc:
        print(f"updated {result.rc_path}")
    else:
        print(f"{result.rc_path} already sources Commas")
    print(f"restart your shell or run: source {result.rc_path}")
    return 0


@cli.command(
    "doctor",
    epilog=examples(
        "commas doctor",
        "commas doctor --json",
    ),
)
@click.option("--json", "json_output", is_flag=True, help="Emit doctor checks as JSON.")
def cmd_doctor(json_output: bool) -> int:
    """Check whether Commas is installed and ready to use.

    Checks the install, shell binding, state directory, current session,
    and the model endpoint, plus codex credentials when a codex profile
    is configured. Exits 1 when any check fails, and still prints the
    full report.
    """
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
    "commas:installed": "commas installed?",
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
