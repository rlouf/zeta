"""Local project provider discovery."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from typing import Iterable, Mapping

from .declarations import ProviderKind, ProviderRegistration, provider_registration


class DiscoveryError(RuntimeError):
    """Zeta could not load or catalog a provider."""


@dataclass(frozen=True)
class ProviderSource:
    """The source module for one discovered provider."""

    module: str
    path: Path


@dataclass(frozen=True)
class LoadedProvider:
    """A provider implementation with its declaration and source."""

    registration: ProviderRegistration
    source: ProviderSource

    @property
    def identifier(self) -> str:
        """Get the provider identifier."""

        return self.registration.declaration.identifier


class ProviderCatalog:
    """The unique providers that a discovery scope exports."""

    def __init__(self) -> None:
        self._providers: dict[ProviderKind, dict[str, LoadedProvider]] = {
            kind: {} for kind in ProviderKind
        }

    def add(self, provider: LoadedProvider) -> None:
        """Add a provider or fail for a duplicate identifier."""

        declaration = provider.registration.declaration
        providers = self._providers[declaration.kind]
        previous = providers.get(declaration.identifier)
        if previous is not None:
            raise DiscoveryError(
                f"Duplicate {declaration.kind.value} provider {declaration.identifier!r}: "
                f"{previous.source.path} and {provider.source.path}"
            )
        providers[declaration.identifier] = provider

    @property
    def models(self) -> Mapping[str, LoadedProvider]:
        """Get the model providers."""

        return self._providers[ProviderKind.MODEL].copy()

    @property
    def tools(self) -> Mapping[str, LoadedProvider]:
        """Get the tool providers."""

        return self._providers[ProviderKind.TOOL].copy()

    @property
    def connectors(self) -> Mapping[str, LoadedProvider]:
        """Get the connector providers."""

        return self._providers[ProviderKind.CONNECTOR].copy()

    def providers(self, kind: ProviderKind) -> Mapping[str, LoadedProvider]:
        """Get the providers for one category."""

        return self._providers[kind].copy()


_DIRECTORIES: dict[ProviderKind, str] = {
    ProviderKind.MODEL: "models",
    ProviderKind.TOOL: "tools",
    ProviderKind.CONNECTOR: "connectors",
}


def discover_project(project_root: Path) -> ProviderCatalog:
    """Load provider modules from a project's conventional directories."""

    root = project_root.resolve()
    if not root.is_dir():
        raise DiscoveryError(f"The project root does not exist: {root}")

    package_name = f"_zeta_project_{hashlib.sha256(str(root).encode()).hexdigest()[:16]}"
    _ensure_package(package_name, root)
    catalog = ProviderCatalog()

    for kind, directory_name in _DIRECTORIES.items():
        directory = root / directory_name
        if not directory.is_dir():
            continue
        for path in _provider_files(directory):
            module_name = _module_name(package_name, root, path)
            module = _load_module(module_name, path)
            source = ProviderSource(module=module_name, path=path)
            for registration in _module_registrations(module):
                if registration.declaration.kind is not kind:
                    raise DiscoveryError(
                        f"Provider {registration.declaration.identifier!r} in {path} has "
                        f"category {registration.declaration.kind.value!r}; expected "
                        f"{kind.value!r}"
                    )
                catalog.add(LoadedProvider(registration=registration, source=source))

    return catalog


def _provider_files(directory: Path) -> Iterable[Path]:
    for path in sorted(directory.rglob("*.py")):
        relative = path.relative_to(directory)
        if any(part.startswith("_") for part in relative.parts):
            continue
        yield path


def _module_name(package_name: str, root: Path, path: Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    components = relative.parts
    if not all(component.isidentifier() for component in components):
        raise DiscoveryError(f"The provider module path is not a Python module: {path}")
    return ".".join((package_name, *components))


def _ensure_package(package_name: str, path: Path) -> ModuleType:
    existing = sys.modules.get(package_name)
    if existing is not None:
        return existing

    package = ModuleType(package_name)
    package.__path__ = [str(path)]  # type: ignore[attr-defined]
    package.__package__ = package_name
    sys.modules[package_name] = package
    return package


def _load_module(module_name: str, path: Path) -> ModuleType:
    parent_name, _, _ = module_name.rpartition(".")
    parent_path = path.parent
    _ensure_parent_packages(parent_name, parent_path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise DiscoveryError(f"Zeta could not load provider module: {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        raise DiscoveryError(f"Zeta could not import provider module {path}: {error}") from error
    return module


def _ensure_parent_packages(package_name: str, path: Path) -> None:
    components = package_name.split(".")
    current_path = path
    paths: list[Path] = []
    for _ in components:
        paths.append(current_path)
        current_path = current_path.parent

    for index in range(len(components)):
        name = ".".join(components[: index + 1])
        _ensure_package(name, paths[-(index + 1)])


def _module_registrations(module: ModuleType) -> Iterable[ProviderRegistration]:
    registrations: set[int] = set()
    for _, target in sorted(vars(module).items()):
        if getattr(target, "__module__", None) != module.__name__:
            continue
        registration = provider_registration(target)
        if registration is None or id(registration) in registrations:
            continue
        registrations.add(id(registration))
        yield registration
