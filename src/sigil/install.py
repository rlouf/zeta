"""Install and diagnose Sigil shell integrations."""

from __future__ import annotations

import importlib.resources
import json
import os
import shlex
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .state import state_dir
from .zeta.model import (
    endpoint_reachable,
    model_endpoint_valid,
    model_url,
    model_url_from_env,
)


BINDING_NAME = "sigil.zsh"


@dataclass(frozen=True)
class InstallResult:
    binding_path: str
    rc_path: str
    source_path: str
    wrote_rc: bool
    glyphs_enabled: bool


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    detail: str
    hint: str | None = None


def binding_source() -> Path:
    """Return the zsh binding source from package data or a source checkout."""
    packaged = importlib.resources.files("sigil").joinpath("shell", "zsh", BINDING_NAME)
    try:
        with importlib.resources.as_file(packaged) as path:
            if path.exists():
                return path
    except FileNotFoundError:
        pass

    source_checkout = Path(__file__).resolve().parent / "shell" / "zsh" / BINDING_NAME
    if source_checkout.exists():
        return source_checkout
    raise FileNotFoundError(BINDING_NAME)


def default_install_dir(env: dict[str, str] | None = None) -> Path:
    """Return the install directory for the zsh binding."""
    values = env if env is not None else os.environ
    override = values.get("SIGIL_SHELL_DIR")
    if override:
        return Path(override)
    return Path.home() / ".sigil" / "shell" / "zsh"


def default_rc_path(env: dict[str, str] | None = None) -> Path:
    """Return the default zsh rc file path."""
    values = env if env is not None else os.environ
    return Path(values.get("ZDOTDIR") or Path.home()) / ".zshrc"


def shell_reference(path: Path) -> str:
    """Return a shell-friendly reference to a path."""
    home = Path.home()
    try:
        relative = path.resolve().relative_to(home.resolve())
    except ValueError:
        return shlex.quote(str(path))
    return '"$HOME/' + relative.as_posix() + '"'


def source_snippet(
    binding_path: Path,
    *,
    enable_glyphs: bool = True,
    sigil_bin: str | None = None,
    zeta_bin: str | None = None,
) -> str:
    """Return the rc block that loads a Sigil shell binding."""
    reference = shell_reference(binding_path)
    lines = ["", "# Sigil", f"if [[ -r {reference} ]]; then"]
    if sigil_bin:
        lines.append(f"  export SIGIL_BIN={shlex.quote(sigil_bin)}")
    if zeta_bin:
        lines.append("  # Used by sigil doctor for bundled runtime service discovery.")
        lines.append(f"  export ZETA_BIN={shlex.quote(zeta_bin)}")
    if not enable_glyphs:
        lines.append("  export SIGIL_ENABLE_GLYPHS=0")
    else:
        lines.append("  export SIGIL_ENABLE_GLYPHS=1")
    lines.append(f"  source {reference}")
    lines.append("fi")
    return "\n".join(lines) + "\n"


def replace_sigil_source_block(
    rc_text: str,
    references: set[str],
    snippet: str,
) -> tuple[str, bool]:
    """Replace an existing Sigil rc block that sources this binding."""
    lines = rc_text.splitlines(keepends=True)
    for start, line in enumerate(lines):
        if line.strip() != "# Sigil":
            continue
        for stop in range(start + 1, len(lines)):
            if lines[stop].strip() != "fi":
                continue
            block = "".join(lines[start : stop + 1])
            if not any(reference in block for reference in references):
                break
            if block == snippet.lstrip("\n"):
                return rc_text, False
            updated = [*lines[:start], snippet, *lines[stop + 1 :]]
            return "".join(updated), True
    return rc_text, False


def install_shell(
    install_dir: Path | None = None,
    rc_path: Path | None = None,
    *,
    enable_glyphs: bool = True,
) -> InstallResult:
    """Install or update the zsh binding and idempotently source it from rc."""
    install_root = install_dir or default_install_dir()
    rc = rc_path or default_rc_path()
    binding_path = install_root / BINDING_NAME

    source = binding_source()
    install_root.mkdir(parents=True, exist_ok=True)
    binding_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    binding_path.chmod(0o644)

    snippet = source_snippet(
        binding_path,
        enable_glyphs=enable_glyphs,
        sigil_bin=shutil.which("sigil"),
        zeta_bin=shutil.which("zeta"),
    )
    rc.parent.mkdir(parents=True, exist_ok=True)
    rc.touch(exist_ok=True)
    rc_text = rc.read_text(encoding="utf-8")
    references = {str(binding_path), f"$HOME/.sigil/shell/zsh/{BINDING_NAME}"}
    wrote_rc = False
    rc_text, wrote_rc = replace_sigil_source_block(rc_text, references, snippet)
    if wrote_rc:
        rc.write_text(rc_text, encoding="utf-8")
    elif not any(reference in rc_text for reference in references):
        with rc.open("a", encoding="utf-8") as f:
            f.write(snippet)
        wrote_rc = True

    return InstallResult(
        binding_path=str(binding_path),
        rc_path=str(rc),
        source_path=str(source),
        wrote_rc=wrote_rc,
        glyphs_enabled=enable_glyphs,
    )


def detect_shell(env: dict[str, str] | None = None) -> str | None:
    """Detect the current login shell from the environment."""
    values = env if env is not None else os.environ
    shell = values.get("SHELL")
    if not shell:
        return None
    return Path(shell).name


def check_executable(name: str, hint: str | None = None) -> DoctorCheck:
    """Check that an executable is available on PATH."""
    path = shutil.which(name)
    if path:
        return DoctorCheck(name=f"executable:{name}", status="ok", detail=path)
    return DoctorCheck(
        name=f"executable:{name}",
        status="fail",
        detail=f"{name} is not on PATH",
        hint=hint or f"Install {name} or update PATH.",
    )


def check_configured_executable(
    command_name: str,
    env_name: str,
    *,
    hint: str | None = None,
) -> DoctorCheck:
    """Check a command using an explicit env override before PATH."""
    configured = os.environ.get(env_name)
    if configured:
        path = Path(configured)
        if path.exists() and os.access(path, os.X_OK):
            return DoctorCheck(
                name=f"executable:{command_name}",
                status="ok",
                detail=f"{configured} from {env_name}",
            )
        return DoctorCheck(
            name=f"executable:{command_name}",
            status="fail",
            detail=f"{env_name} points to a missing or non-executable path: {configured}",
            hint=hint or f"Update {env_name} or install {command_name} on PATH.",
        )
    return check_executable(command_name, hint=hint)


def check_sigil_installed() -> DoctorCheck:
    """Check that the public Sigil command is available."""
    check = check_configured_executable("sigil", "SIGIL_BIN")
    return DoctorCheck(
        name="sigil:installed",
        status=check.status,
        detail=check.detail,
        hint=check.hint,
    )


def check_zeta_installed() -> DoctorCheck:
    """Check that the Zeta service command is available."""
    check = check_configured_executable(
        "zeta",
        "ZETA_BIN",
        hint=(
            "Install Sigil with the zeta entrypoint or set ZETA_BIN as a "
            "runtime-service override."
        ),
    )
    return DoctorCheck(
        name="zeta:installed",
        status=check.status,
        detail=check.detail,
        hint=check.hint,
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
    model_endpoint = model_url() if env is None else model_url_from_env(env)
    if not model_endpoint_valid(model_endpoint):
        return DoctorCheck(
            "model:endpoint",
            "fail",
            f"invalid ZETA_MODEL_URL: {model_endpoint}",
            "Set ZETA_MODEL_URL to an OpenAI-compatible chat completions endpoint.",
        )
    if endpoint_reachable(model_endpoint):
        return DoctorCheck("model:endpoint", "ok", model_endpoint)
    return DoctorCheck(
        "model:endpoint",
        "warn",
        f"not reachable at {model_endpoint}",
        "Start the local model server or set ZETA_MODEL_URL.",
    )


def check_shell_support(shell: str | None) -> DoctorCheck:
    """Check that the current shell is zsh, the only supported shell."""
    if shell == "zsh":
        return DoctorCheck("shell:supported", "ok", shell)
    if shell is None:
        return DoctorCheck(
            "shell:supported",
            "warn",
            "SHELL is not set",
            "Sigil supports zsh; run it in zsh.",
        )
    return DoctorCheck(
        "shell:supported",
        "fail",
        f"{shell} is not supported",
        "Sigil supports zsh; run it in zsh.",
    )


def check_shell_binding_installed() -> DoctorCheck:
    """Check that the zsh binding exists in the install location."""
    path = default_install_dir() / BINDING_NAME
    if path.exists():
        return DoctorCheck("shell:binding-installed", "ok", str(path))
    return DoctorCheck(
        "shell:binding-installed",
        "fail",
        f"{path} does not exist",
        "Run sigil install.",
    )


def check_shell_binding_loaded(env: dict[str, str] | None = None) -> DoctorCheck:
    """Check whether the current process looks like it inherited a binding."""
    values = env if env is not None else os.environ
    binding = values.get("SIGIL_BINDING_LOADED")
    if binding:
        return DoctorCheck("shell:binding-loaded", "ok", f"{binding} binding loaded")
    session_id = values.get("SIGIL_SESSION_ID")
    if session_id:
        return DoctorCheck("shell:binding-loaded", "ok", f"session {session_id}")
    return DoctorCheck(
        "shell:binding-loaded",
        "warn",
        "SIGIL_SESSION_ID is not set",
        "Restart the shell or source the Sigil binding.",
    )


def check_glyphs_enabled(env: dict[str, str] | None = None) -> DoctorCheck:
    """Check whether glyph functions are enabled for the loaded binding."""
    values = env if env is not None else os.environ
    if not values.get("SIGIL_SESSION_ID"):
        return DoctorCheck(
            "shell:glyphs-enabled",
            "warn",
            "unknown because the shell binding is not loaded",
            "Restart the shell or source the Sigil binding.",
        )
    enabled = values.get("SIGIL_ENABLE_GLYPHS", "1").lower() not in {"0", "false"}
    if enabled:
        return DoctorCheck("shell:glyphs-enabled", "ok", "glyphs enabled")
    return DoctorCheck(
        "shell:glyphs-enabled",
        "warn",
        "glyphs disabled",
        "Run sigil install --glyphs, then restart.",
    )


def doctor_checks() -> list[DoctorCheck]:
    """Run Sigil environment checks."""
    checks = [
        check_sigil_installed(),
        check_zeta_installed(),
        check_endpoint(),
        check_shell_binding_installed(),
        check_shell_binding_loaded(),
        check_glyphs_enabled(),
        check_shell_support(detect_shell()),
        check_state_writable(),
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
