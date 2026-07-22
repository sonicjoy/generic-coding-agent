"""Declarative model catalog loading from ``models.yaml``.

Providers and models are configured without Python plugins. API keys are never
stored in the file; each provider names the environment variable that holds its
secret. Catalog files are merged in this order (later wins):

1. ``~/.gca/models.yaml``
2. ``<workspace>/models.yaml``
3. ``<workspace>/.gca/models.yaml``
4. any paths supplied via ``--models``
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from gca.models import DEFAULT_CAPABILITIES, ModelProfile, ModelRegistry
from gca.providers.openai_compatible import OpenAICompatibleProvider

KNOWN_PROVIDER_TYPES = frozenset({"openai_compatible"})


class ModelConfigError(ValueError):
    """Raised when a models.yaml catalog is invalid."""


@dataclass
class ProviderSpec:
    """Named provider endpoint used by one or more models."""

    name: str
    type: str
    base_url: str
    api_key_env: str
    timeout: int = 180
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class ModelSpec:
    """Named model registration metadata from models.yaml."""

    name: str
    provider: str
    model_id: str
    strength: int = 3
    speed: int = 3
    cost: int = 3
    capabilities: frozenset[str] = field(default_factory=lambda: DEFAULT_CAPABILITIES)


@dataclass
class ModelCatalog:
    """Merged provider and model definitions."""

    providers: dict[str, ProviderSpec] = field(default_factory=dict)
    models: dict[str, ModelSpec] = field(default_factory=dict)

    def merge(self, other: ModelCatalog) -> ModelCatalog:
        """Return a new catalog with ``other`` overriding this one."""

        return ModelCatalog(
            providers={**self.providers, **other.providers},
            models={**self.models, **other.models},
        )


def default_model_config_paths(workspace: Path) -> list[Path]:
    """Return the default search paths for model catalogs."""

    return [
        Path.home() / ".gca" / "models.yaml",
        Path(workspace).resolve() / "models.yaml",
        Path(workspace).resolve() / ".gca" / "models.yaml",
    ]


def load_model_catalog(paths: list[Path]) -> ModelCatalog:
    """Load and merge model catalogs from the given paths."""

    catalog = ModelCatalog()
    for path in paths:
        path = Path(path)
        if not path.is_file():
            continue
        catalog = catalog.merge(_parse_catalog_file(path))
    return catalog


def build_registry_from_catalog(catalog: ModelCatalog) -> ModelRegistry:
    """Instantiate providers and register models from a catalog."""

    registry = ModelRegistry()
    for model in catalog.models.values():
        provider_spec = catalog.providers.get(model.provider)
        if provider_spec is None:
            available = ", ".join(sorted(catalog.providers)) or "none"
            raise ModelConfigError(
                f"model '{model.name}' references unknown provider "
                f"'{model.provider}' (available: {available})"
            )
        provider = _build_provider(provider_spec, model.model_id)
        registry.register(
            ModelProfile(
                name=model.name,
                provider=provider,
                strength=model.strength,
                speed=model.speed,
                cost=model.cost,
                capabilities=model.capabilities,
                model_id=model.model_id,
            )
        )
    return registry


def load_dotenv(path: Path) -> None:
    """Load ``KEY=VALUE`` pairs from ``path`` without overriding existing env vars."""

    path = Path(path)
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def _parse_catalog_file(path: Path) -> ModelCatalog:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ModelConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ModelConfigError(f"{path} must contain a mapping at the top level")

    allowed = {"providers", "models"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ModelConfigError(f"unknown keys in {path}: {', '.join(unknown)}")

    providers_raw = raw.get("providers", {})
    models_raw = raw.get("models", {})
    if not isinstance(providers_raw, Mapping):
        raise ModelConfigError(f"'providers' in {path} must be a mapping")
    if not isinstance(models_raw, Mapping):
        raise ModelConfigError(f"'models' in {path} must be a mapping")

    providers = {
        str(name): _parse_provider(str(name), value, path) for name, value in providers_raw.items()
    }
    models = {str(name): _parse_model(str(name), value, path) for name, value in models_raw.items()}
    return ModelCatalog(providers=providers, models=models)


def _parse_provider(name: str, value: object, path: Path) -> ProviderSpec:
    if not isinstance(value, Mapping):
        raise ModelConfigError(f"provider '{name}' in {path} must be a mapping")
    raw = dict(value)
    allowed = {"type", "base_url", "api_key_env", "timeout", "headers"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ModelConfigError(
            f"unknown keys for provider '{name}' in {path}: {', '.join(unknown)}"
        )
    provider_type = str(raw.get("type", "openai_compatible"))
    if provider_type not in KNOWN_PROVIDER_TYPES:
        raise ModelConfigError(
            f"unsupported provider type '{provider_type}' for '{name}' in {path}"
        )
    base_url = raw.get("base_url")
    api_key_env = raw.get("api_key_env")
    if not isinstance(base_url, str) or not base_url.strip():
        raise ModelConfigError(f"provider '{name}' in {path} requires base_url")
    if not isinstance(api_key_env, str) or not api_key_env.strip():
        raise ModelConfigError(f"provider '{name}' in {path} requires api_key_env")
    timeout = raw.get("timeout", 180)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ModelConfigError(f"provider '{name}' timeout must be a positive integer")
    headers_raw = raw.get("headers", {})
    if not isinstance(headers_raw, Mapping) or not all(
        isinstance(key, str) and isinstance(val, str) for key, val in headers_raw.items()
    ):
        raise ModelConfigError(f"provider '{name}' headers must be a string mapping")
    return ProviderSpec(
        name=name,
        type=provider_type,
        base_url=base_url,
        api_key_env=api_key_env,
        timeout=timeout,
        headers={str(key): str(val) for key, val in headers_raw.items()},
    )


def _parse_model(name: str, value: object, path: Path) -> ModelSpec:
    if not isinstance(value, Mapping):
        raise ModelConfigError(f"model '{name}' in {path} must be a mapping")
    raw = dict(value)
    allowed = {
        "provider",
        "model_id",
        "strength",
        "speed",
        "cost",
        "capabilities",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ModelConfigError(f"unknown keys for model '{name}' in {path}: {', '.join(unknown)}")
    provider = raw.get("provider")
    model_id = raw.get("model_id")
    if not isinstance(provider, str) or not provider.strip():
        raise ModelConfigError(f"model '{name}' in {path} requires provider")
    if not isinstance(model_id, str) or not model_id.strip():
        raise ModelConfigError(f"model '{name}' in {path} requires model_id")
    return ModelSpec(
        name=name,
        provider=provider,
        model_id=model_id,
        strength=_score(raw.get("strength", 3), f"model '{name}' strength"),
        speed=_score(raw.get("speed", 3), f"model '{name}' speed"),
        cost=_score(raw.get("cost", 3), f"model '{name}' cost"),
        capabilities=_capabilities(raw.get("capabilities"), f"model '{name}'"),
    )


def _score(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
        raise ModelConfigError(f"{label} must be an integer from 1 to 5")
    return value


def _capabilities(value: object, label: str) -> frozenset[str]:
    if value is None:
        return DEFAULT_CAPABILITIES
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ModelConfigError(f"{label} capabilities must be a list of strings")
    return frozenset(value)


def _build_provider(spec: ProviderSpec, model_id: str) -> OpenAICompatibleProvider:
    if spec.type != "openai_compatible":
        raise ModelConfigError(f"unsupported provider type: {spec.type}")
    return OpenAICompatibleProvider(
        model_id=model_id,
        base_url=spec.base_url,
        api_key_env=spec.api_key_env,
        timeout=spec.timeout,
        default_headers=spec.headers,
    )
