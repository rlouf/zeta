"""Install and diagnose Sigil shell integrations."""

from __future__ import annotations

import importlib.resources
import json
import os
import shlex
import shutil
import socket
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .state import state_dir


SUPPORTED_SHELLS = ("zsh", "bash")
DEFAULT_QWEN_URL = "http://127.0.0.1:8080/v1/chat/completions"


@dataclass(frozen=True)
class ShellSpec:
    name: str
    binding_name: str


@dataclass(frozen=True)
class InstallResult:
    shell: str
    binding_path: str
    rc_path: str
    source_path: str
    wrote_rc: bool


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    detail: str
    hint: str | None = None


SPECS = {
    "zsh": ShellSpec("zsh", "sigil.zsh"),
    "bash": ShellSpec("bash", "sigil.bash"),
}


def binding_source(shell: str) -> Path:
    """Return the source binding path from package data or a source checkout."""
    spec = SPECS[shell]
    packaged = importlib.resources.files("sigil").joinpath(
        "shell", shell, spec.binding_name
    )
    try:
        with importlib.resources.as_file(packaged) as path:
            if path.exists():
                return path
    except FileNotFoundError:
        pass

    source_checkout = Path(__file__).resolve().parents[2] / "shell" / shell
    source_checkout = source_checkout / spec.binding_name
    if source_checkout.exists():
        return source_checkout
    raise FileNotFoundError(spec.binding_name)


def default_install_dir(shell: str, env: dict[str, str] | None = None) -> Path:
    """Return the install directory for a shell binding."""
    values = env if env is not None else os.environ
    override = values.get("SIGIL_SHELL_DIR")
    if override:
        return Path(override)
    return Path.home() / ".sigil" / "shell" / shell


def default_rc_path(shell: str, env: dict[str, str] | None = None) -> Path:
    """Return the default rc file path for a shell."""
    values = env if env is not None else os.environ
    if shell == "zsh":
        return Path(values.get("ZDOTDIR") or Path.home()) / ".zshrc"
    if shell == "bash":
        return Path(values.get("SIGIL_BASH_RC") or Path.home() / ".bashrc")
    raise ValueError(f"unsupported shell: {shell}")


def shell_reference(path: Path) -> str:
    """Return a shell-friendly reference to a path."""
    home = Path.home()
    try:
        relative = path.resolve().relative_to(home.resolve())
    except ValueError:
        return shlex.quote(str(path))
    return '"$HOME/' + relative.as_posix() + '"'


def source_snippet(binding_path: Path) -> str:
    """Return the rc block that loads a Sigil shell binding."""
    reference = shell_reference(binding_path)
    return f"\n# Sigil\nif [[ -r {reference} ]]; then\n  source {reference}\nfi\n"


def install_shell(
    shell: str,
    install_dir: Path | None = None,
    rc_path: Path | None = None,
) -> InstallResult:
    """Install or update a shell binding and idempotently source it from rc."""
    if shell not in SPECS:
        raise ValueError(f"unsupported shell: {shell}")
    spec = SPECS[shell]
    install_root = install_dir or default_install_dir(shell)
    rc = rc_path or default_rc_path(shell)
    binding_path = install_root / spec.binding_name

    source = binding_source(shell)
    install_root.mkdir(parents=True, exist_ok=True)
    binding_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    binding_path.chmod(0o644)

    snippet = source_snippet(binding_path)
    rc.parent.mkdir(parents=True, exist_ok=True)
    rc.touch(exist_ok=True)
    rc_text = rc.read_text(encoding="utf-8")
    references = {str(binding_path), f"$HOME/.sigil/shell/{shell}/{spec.binding_name}"}
    wrote_rc = False
    if not any(reference in rc_text for reference in references):
        with rc.open("a", encoding="utf-8") as f:
            f.write(snippet)
        wrote_rc = True

    return InstallResult(
        shell=shell,
        binding_path=str(binding_path),
        rc_path=str(rc),
        source_path=str(source),
        wrote_rc=wrote_rc,
    )


def detect_shell(env: dict[str, str] | None = None) -> str | None:
    """Detect the current login shell from the environment."""
    values = env if env is not None else os.environ
    shell = values.get("SHELL")
    if not shell:
        return None
    name = Path(shell).name
    return name if name in SUPPORTED_SHELLS else name


def check_executable(name: str) -> DoctorCheck:
    """Check that an executable is available on PATH."""
    path = shutil.which(name)
    if path:
        return DoctorCheck(name=f"executable:{name}", status="ok", detail=path)
    return DoctorCheck(
        name=f"executable:{name}",
        status="fail",
        detail=f"{name} is not on PATH",
        hint=f"Install {name} or update PATH.",
    )


def check_state_writable() -> DoctorCheck:
    """Check that Sigil can write its state directory."""
    root = state_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".doctor-", dir=root, delete=True) as f:
            f.write(b"ok")
        return DoctorCheck("state:writable", "ok", str(root))
    except OSError as error:
        return DoctorCheck(
            "state:writable",
            "fail",
            f"{root}: {error}",
            "Check permissions or set SIGIL_STATE_DIR to a writable directory.",
        )


def check_endpoint(env: dict[str, str] | None = None) -> DoctorCheck:
    """Check whether the configured local model endpoint accepts TCP."""
    values = env if env is not None else os.environ
    qwen_url = values.get("QWEN_URL") or DEFAULT_QWEN_URL
    parsed = urlparse(qwen_url)
    host = parsed.hostname
    if host is None:
        return DoctorCheck(
            "model:endpoint",
            "fail",
            f"invalid QWEN_URL: {qwen_url}",
            "Set QWEN_URL to an OpenAI-compatible chat completions endpoint.",
        )
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=0.5):
            pass
        return DoctorCheck("model:endpoint", "ok", qwen_url)
    except OSError:
        return DoctorCheck(
            "model:endpoint",
            "warn",
            f"not reachable at {qwen_url}",
            "Start the local model server or set QWEN_URL.",
        )


def check_model_config(env: dict[str, str] | None = None) -> DoctorCheck:
    """Check whether model identity is configured."""
    values = env if env is not None else os.environ
    model = values.get("QWEN_MODEL")
    if model:
        return DoctorCheck("model:name", "ok", model)
    return DoctorCheck(
        "model:name",
        "warn",
        "QWEN_MODEL is not set",
        "Set QWEN_MODEL if the endpoint requires an explicit model name.",
    )


def check_shell_support(shell: str | None) -> DoctorCheck:
    """Check that the selected shell is supported."""
    if shell in SUPPORTED_SHELLS:
        return DoctorCheck("shell:supported", "ok", shell)
    if shell is None:
        return DoctorCheck(
            "shell:supported",
            "warn",
            "SHELL is not set",
            "Run sigil doctor --shell zsh or --shell bash.",
        )
    return DoctorCheck(
        "shell:supported",
        "fail",
        f"{shell} is not supported",
        "Use zsh or bash, or add a new shell binding.",
    )


def check_shell_binding_installed(shell: str | None) -> DoctorCheck:
    """Check that the selected shell binding exists in the install location."""
    if shell not in SUPPORTED_SHELLS:
        return DoctorCheck(
            "shell:binding-installed",
            "warn",
            "skipped because shell is unsupported or unknown",
        )
    spec = SPECS[shell]
    path = default_install_dir(shell) / spec.binding_name
    if path.exists():
        return DoctorCheck("shell:binding-installed", "ok", str(path))
    return DoctorCheck(
        "shell:binding-installed",
        "fail",
        f"{path} does not exist",
        f"Run sigil install {shell}.",
    )


def check_shell_binding_loaded(env: dict[str, str] | None = None) -> DoctorCheck:
    """Check whether the current process looks like it inherited a binding."""
    values = env if env is not None else os.environ
    session_id = values.get("SIGIL_SESSION_ID")
    if session_id:
        return DoctorCheck("shell:binding-loaded", "ok", f"session {session_id}")
    return DoctorCheck(
        "shell:binding-loaded",
        "warn",
        "SIGIL_SESSION_ID is not set",
        "Restart the shell or source the Sigil binding.",
    )


def doctor_checks(shell: str | None = None) -> list[DoctorCheck]:
    """Run Sigil environment checks."""
    selected_shell = detect_shell() if shell in (None, "auto") else shell
    checks = [
        check_executable("sigil"),
        check_executable("fzf"),
        check_executable("glow"),
        check_executable("pi"),
        check_endpoint(),
        check_model_config(),
        check_state_writable(),
        check_shell_support(selected_shell),
        check_shell_binding_installed(selected_shell),
        check_shell_binding_loaded(),
    ]
    return checks


def checks_to_json(checks: list[DoctorCheck]) -> str:
    """Serialize doctor checks as stable JSON."""
    return json.dumps([asdict(check) for check in checks], ensure_ascii=False, indent=2)


def checks_exit_code(checks: list[DoctorCheck]) -> int:
    """Return a process exit code for doctor results."""
    return 1 if any(check.status == "fail" for check in checks) else 0


def checks_summary(checks: list[DoctorCheck]) -> dict[str, Any]:
    """Return aggregate doctor counts."""
    return {
        "ok": sum(1 for check in checks if check.status == "ok"),
        "warn": sum(1 for check in checks if check.status == "warn"),
        "fail": sum(1 for check in checks if check.status == "fail"),
    }
