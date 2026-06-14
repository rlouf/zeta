"""Model profile discovery and session selection for Zeta."""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

ACTIVE_MODEL_STATE = "active-model.json"
MODEL_NAME_PATTERN = re.compile(r"^[a-z0-9-]+$")
DEFAULT_MODEL_URL = "http://127.0.0.1:8080/v1/chat/completions"
DEFAULT_MODEL_NAME = "local-model"
DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api"

ModelSource = Literal["session", "config", "builtin"]

THINKING_EFFORTS = ("none", "minimal", "low", "medium", "high")

CHAT_COMPLETIONS_API = "chat-completions"
CODEX_RESPONSES_API = "codex-responses"
MODEL_APIS = (CHAT_COMPLETIONS_API, CODEX_RESPONSES_API)
_SESSION_DIR_FACTORY: SessionDirFactory | None = None


class SessionDirFactory(Protocol):
    def __call__(self, session_id: str | None = None) -> Path: ...


def set_profile_session_dir_factory(factory: SessionDirFactory | None) -> None:
    global _SESSION_DIR_FACTORY
    _SESSION_DIR_FACTORY = factory


def profile_session_dir() -> Path:
    if _SESSION_DIR_FACTORY is not None:
        return _SESSION_DIR_FACTORY(None)
    root = os.environ.get("ZETA_STATE_DIR")
    session = os.environ.get("ZETA_SESSION_ID") or "default"
    base = Path(root).expanduser() if root else Path.home() / ".zeta"
    return base / "sessions" / session


def active_model_state_path() -> Path:
    return profile_session_dir() / ACTIVE_MODEL_STATE


@dataclass(frozen=True)
class ModelProfile:
    name: str
    model: str
    url: str | None = None
    thinking: str | None = None
    api: str | None = None
    default: bool = False


@dataclass(frozen=True)
class ModelDiagnostic:
    path: Path
    message: str


@dataclass(frozen=True)
class ModelCatalog:
    profiles: dict[str, ModelProfile] = field(default_factory=dict)
    diagnostics: list[ModelDiagnostic] = field(default_factory=list)
    default_profile: str | None = None


@dataclass(frozen=True)
class ModelSelection:
    profile: str
    model: str
    url: str
    thinking: str | None = None
    api: str = CHAT_COMPLETIONS_API


@dataclass(frozen=True)
class ModelResolution:
    selection: ModelSelection
    source: ModelSource
    stale_profile: str | None = None


def model_url(selected_url: str | None = None) -> str:
    """Return the OpenAI-compatible chat completions endpoint."""
    return selected_url or DEFAULT_MODEL_URL


def model_name(selected_model: str | None = None) -> str:
    """Return the model name sent to the configured endpoint."""
    return selected_model or DEFAULT_MODEL_NAME


def user_models_config_path() -> Path:
    """Return the user model profile config path."""
    return Path.home() / ".zeta" / "models.toml"


def load_model_profiles(config_path: Path | None = None) -> ModelCatalog:
    """Load configured model profiles from ``~/.zeta/models.toml``."""
    path = config_path or user_models_config_path()
    if not path.exists():
        return ModelCatalog()
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return ModelCatalog(
            diagnostics=[ModelDiagnostic(path, f"could not read {path}: {exc}")]
        )
    raw_profiles = config.get("models", [])
    if not isinstance(raw_profiles, list):
        return ModelCatalog(
            diagnostics=[ModelDiagnostic(path, "models must be an array of tables")]
        )
    profiles: dict[str, ModelProfile] = {}
    diagnostics: list[ModelDiagnostic] = []
    default_profile: str | None = None
    for index, raw_profile in enumerate(raw_profiles):
        profile, diagnostic = _parse_profile(path, index, raw_profile)
        if diagnostic is not None:
            diagnostics.append(diagnostic)
            continue
        if profile is None:
            continue
        profiles[profile.name] = profile
        if not profile.default:
            continue
        if default_profile is None:
            default_profile = profile.name
        else:
            diagnostics.append(
                ModelDiagnostic(
                    path,
                    f"only one profile may set default = true; keeping "
                    f"{default_profile}, ignoring {profile.name}",
                )
            )
    return ModelCatalog(
        profiles=profiles,
        diagnostics=diagnostics,
        default_profile=default_profile,
    )


def resolve_model_profile(
    name: str,
    *,
    catalog: ModelCatalog | None = None,
) -> ModelSelection | None:
    """Resolve a named profile to the concrete model request fields."""
    catalog = catalog or load_model_profiles()
    profile = catalog.profiles.get(name)
    if profile is None:
        return None
    api = profile.api or CHAT_COMPLETIONS_API
    if profile.url:
        url = profile.url
    elif api == CODEX_RESPONSES_API:
        url = DEFAULT_CODEX_BASE_URL
    else:
        url = model_url()
    return ModelSelection(
        profile=profile.name,
        model=profile.model,
        url=url,
        thinking=profile.thinking,
        api=api,
    )


def active_model_profile() -> str | None:
    """Return the active model profile name for this session, if set."""
    state = read_json(active_model_state_path())
    if not isinstance(state, dict):
        return None
    profile = state.get("profile")
    if not isinstance(profile, str) or not profile:
        return None
    return profile


def active_model_selection() -> ModelSelection | None:
    """Return the session's model, falling back to the configured default."""
    catalog = load_model_profiles()
    profile = active_model_profile()
    if profile is not None:
        selection = resolve_model_profile(profile, catalog=catalog)
        if selection is not None:
            return selection
    return configured_default_selection(catalog)


def configured_default_selection(
    catalog: ModelCatalog | None = None,
) -> ModelSelection | None:
    """Resolve the profile marked ``default = true``, if any."""
    catalog = catalog or load_model_profiles()
    if catalog.default_profile is None:
        return None
    return resolve_model_profile(catalog.default_profile, catalog=catalog)


def set_active_model_profile(name: str) -> None:
    """Store the active model profile name for this session."""
    write_json(active_model_state_path(), {"profile": name})


def clear_active_model_profile() -> bool:
    """Clear the active model profile for this session."""
    return remove_json(active_model_state_path())


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f"{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(handle.name, path)


def remove_json(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def default_model_selection() -> ModelSelection:
    """Return a display-friendly selection for the default environment model."""
    return ModelSelection(profile="default", model=model_name(), url=model_url())


def resolve_active_model() -> ModelResolution:
    """Resolve the model the next request will use, and where it came from.

    Session selection wins, then the profile marked ``default = true``,
    then the builtin local endpoint. A session selection naming a profile
    that no longer resolves falls through but carries the stale name, so
    status surfaces can say the selection went stale instead of
    pretending none was made.
    """
    catalog = load_model_profiles()
    profile = active_model_profile()
    stale_profile: str | None = None
    if profile is not None:
        selection = resolve_model_profile(profile, catalog=catalog)
        if selection is not None:
            return ModelResolution(selection=selection, source="session")
        stale_profile = profile
    configured = configured_default_selection(catalog)
    if configured is not None:
        return ModelResolution(
            selection=configured,
            source="config",
            stale_profile=stale_profile,
        )
    return ModelResolution(
        selection=default_model_selection(),
        source="builtin",
        stale_profile=stale_profile,
    )


def model_selection_event(selection: ModelSelection | None) -> dict[str, str]:
    """Return non-secret model metadata suitable for timeline events."""
    active = selection or default_model_selection()
    return {"profile": active.profile, "model": active.model, "url": active.url}


def _parse_profile(
    path: Path,
    index: int,
    value: Any,
) -> tuple[ModelProfile | None, ModelDiagnostic | None]:
    label = f"models[{index}]"
    if not isinstance(value, dict):
        return None, ModelDiagnostic(path, f"{label} must be a table")
    name = value.get("name")
    if not isinstance(name, str) or not MODEL_NAME_PATTERN.fullmatch(name):
        return (
            None,
            ModelDiagnostic(
                path,
                f"{label}.name must use lowercase letters, digits, and hyphens",
            ),
        )
    model = value.get("model")
    if not isinstance(model, str) or not model.strip():
        return None, ModelDiagnostic(path, f"{label}.model must be a non-empty string")
    url = value.get("url")
    if url is not None and (not isinstance(url, str) or not url.strip()):
        return None, ModelDiagnostic(path, f"{label}.url must be a non-empty string")
    thinking = value.get("thinking")
    if thinking is not None and thinking not in THINKING_EFFORTS:
        return (
            None,
            ModelDiagnostic(
                path,
                f"{label}.thinking must be one of {', '.join(THINKING_EFFORTS)}",
            ),
        )
    api = value.get("api")
    if api is not None and api not in MODEL_APIS:
        return (
            None,
            ModelDiagnostic(
                path,
                f"{label}.api must be one of {', '.join(MODEL_APIS)}",
            ),
        )
    default = value.get("default")
    if default is not None and not isinstance(default, bool):
        return (
            None,
            ModelDiagnostic(path, f"{label}.default must be a boolean"),
        )
    return ModelProfile(
        name=name,
        model=model.strip(),
        url=url.strip() if url else None,
        thinking=thinking,
        api=api,
        default=bool(default),
    ), None
