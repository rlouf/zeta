"""Model profile discovery and session selection for Zeta."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..state import read_json, remove_json, write_json
from .model import model_name, model_url

ACTIVE_MODEL_STATE = "active-model.json"
MODEL_NAME_PATTERN = re.compile(r"^[a-z0-9-]+$")


@dataclass(frozen=True)
class ModelProfile:
    name: str
    model: str
    url: str | None = None


@dataclass(frozen=True)
class ModelDiagnostic:
    path: Path
    message: str


@dataclass(frozen=True)
class ModelCatalog:
    profiles: dict[str, ModelProfile] = field(default_factory=dict)
    diagnostics: list[ModelDiagnostic] = field(default_factory=list)


@dataclass(frozen=True)
class ModelSelection:
    profile: str
    model: str
    url: str


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
    for index, raw_profile in enumerate(raw_profiles):
        profile, diagnostic = _parse_profile(path, index, raw_profile)
        if diagnostic is not None:
            diagnostics.append(diagnostic)
            continue
        if profile is not None:
            profiles[profile.name] = profile
    return ModelCatalog(profiles=profiles, diagnostics=diagnostics)


def resolve_model_profile(
    name: str,
    *,
    catalog: ModelCatalog | None = None,
) -> ModelSelection | None:
    """Resolve a named profile to the concrete model request fields."""
    profiles = catalog or load_model_profiles()
    profile = profiles.profiles.get(name)
    if profile is None:
        return None
    return ModelSelection(
        profile=profile.name,
        model=profile.model,
        url=profile.url or model_url(),
    )


def active_model_profile() -> str | None:
    """Return the active model profile name for this session, if set."""
    state = read_json(ACTIVE_MODEL_STATE)
    if not isinstance(state, dict):
        return None
    profile = state.get("profile")
    if not isinstance(profile, str) or not profile:
        return None
    return profile


def active_model_selection() -> ModelSelection | None:
    """Return the active resolved model for this session, if configured."""
    profile = active_model_profile()
    if profile is None:
        return None
    return resolve_model_profile(profile)


def set_active_model_profile(name: str) -> None:
    """Store the active model profile name for this session."""
    write_json(ACTIVE_MODEL_STATE, {"profile": name})


def clear_active_model_profile() -> bool:
    """Clear the active model profile for this session."""
    return remove_json(ACTIVE_MODEL_STATE)


def default_model_selection() -> ModelSelection:
    """Return a display-friendly selection for the default environment model."""
    return ModelSelection(profile="default", model=model_name(), url=model_url())


def model_selection_event(selection: ModelSelection | None) -> dict[str, str]:
    """Return non-secret model metadata suitable for transcript events."""
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
    return ModelProfile(
        name=name, model=model.strip(), url=url.strip() if url else None
    ), None
