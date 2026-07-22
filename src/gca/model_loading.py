"""Shared model/plugin loading for CLI and hosted job runners."""

from __future__ import annotations

import json
from pathlib import Path

from gca.model_config import (
    ModelConfigError,
    build_registry_from_catalog,
    default_model_config_paths,
    load_dotenv,
    load_model_catalog,
)
from gca.models import ModelProfile, ModelRegistry
from gca.plugins import LoadedPlugins
from gca.providers.scripted import ScriptedProvider
from gca.runtime import RuntimeConfig, load_configured_plugins


def load_runtime_models(
    config: RuntimeConfig,
    *,
    script_path: Path | None = None,
) -> LoadedPlugins:
    """Load effective models and plugins for a runtime configuration."""

    _load_dotenv_files(config)
    loaded = load_configured_plugins(config)
    catalog_paths = list(default_model_config_paths(config.workspace))
    if config.models_paths:
        catalog_paths.extend(config.models_paths)
    try:
        catalog = load_model_catalog(catalog_paths)
        catalog_models = build_registry_from_catalog(catalog)
    except ModelConfigError as exc:
        raise ValueError(f"Invalid models.yaml: {exc}") from exc

    merged = ModelRegistry()
    merged.extend(catalog_models)
    merged.extend(loaded.models)
    loaded.models = merged

    if len(loaded.models) == 0 and script_path is not None:
        data = json.loads(Path(script_path).read_text(encoding="utf-8"))
        provider = ScriptedProvider.from_script(data)
        loaded.models.register(
            ModelProfile(
                name="scripted",
                provider=provider,
                strength=3,
                speed=5,
                cost=1,
            )
        )
    if len(loaded.models) == 0:
        raise ValueError(
            "No models configured. Add models.yaml, configure a provider plugin, "
            "or supply a scripted provider."
        )
    return loaded


def _load_dotenv_files(config: RuntimeConfig) -> None:
    load_dotenv(Path.home() / ".gca" / ".env")
    load_dotenv(config.workspace / ".env")
    load_dotenv(config.workspace / ".gca" / ".env")
