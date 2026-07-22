"""User plugin loading.

Users extend the harness by dropping Python modules into a plugin directory.
A plugin module may expose either:

* a module-level ``TOOLS`` list of :class:`~gca.tools.base.Tool` instances, and/or
* a ``register(registry)`` function that adds tools to the given registry, and/or
* a ``get_models()`` function returning named :class:`~gca.models.ModelProfile`
  registrations, and/or
* a ``get_provider()`` function returning an :class:`~gca.providers.base.LLMProvider`.

Because loading is dynamic (``importlib``), no build step is required — this is
the primary extension path for provider integrations, custom tools, and skills.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from gca.models import ModelProfile, ModelRegistry
from gca.providers.base import LLMProvider
from gca.tools.base import Tool, ToolRegistry


@dataclass
class LoadedPlugins:
    tools: list[Tool] = field(default_factory=list)
    registrars: list[Callable[[ToolRegistry], None]] = field(default_factory=list)
    provider: LLMProvider | None = None
    models: ModelRegistry = field(default_factory=ModelRegistry)
    modules: list[str] = field(default_factory=list)

    def register_tools(self, registry: ToolRegistry) -> None:
        """Register all loaded tool contributions into ``registry``."""

        for tool in self.tools:
            registry.register(tool)
        for register in self.registrars:
            register(registry)


def _load_module(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(f"gca_plugin_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load plugin: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _register_models(value: object, registry: ModelRegistry) -> None:
    if value is None:
        return
    if isinstance(value, Mapping):
        entries: Iterable[tuple[object, object]] = value.items()
        for raw_name, candidate in entries:
            name = str(raw_name)
            if isinstance(candidate, ModelProfile):
                registry.register(replace(candidate, name=name))
            elif isinstance(candidate, LLMProvider):
                registry.register(ModelProfile(name=name, provider=candidate))
            else:
                raise TypeError(f"model '{name}' is not an LLMProvider or ModelProfile")
        return
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        for candidate in value:
            if not isinstance(candidate, ModelProfile):
                raise TypeError("get_models() iterables must contain ModelProfile instances")
            registry.register(candidate)
        return
    raise TypeError("get_models() must return a mapping or iterable of ModelProfile objects")


def load_plugins(directory: Path, registry: ToolRegistry | None = None) -> LoadedPlugins:
    """Load all ``*.py`` plugins from ``directory``.

    Tools discovered via ``TOOLS`` are returned and, if a registry is provided,
    registered into it. Models are accumulated by name. The last plugin that
    defines the legacy ``get_provider`` hook wins and is registered as
    ``"default"``.
    """

    directory = Path(directory)
    result = LoadedPlugins()
    if not directory.is_dir():
        return result

    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module = _load_module(path)
        result.modules.append(path.name)

        module_tools = getattr(module, "TOOLS", [])
        for tool in module_tools:
            if isinstance(tool, Tool):
                result.tools.append(tool)
                if registry is not None:
                    registry.register(tool)

        register_fn = getattr(module, "register", None)
        if callable(register_fn):
            result.registrars.append(register_fn)
            if registry is not None:
                register_fn(registry)

        models_fn = getattr(module, "get_models", None)
        if callable(models_fn):
            _register_models(models_fn(), result.models)

        provider_fn = getattr(module, "get_provider", None)
        if callable(provider_fn):
            provider = provider_fn()
            if isinstance(provider, LLMProvider):
                result.provider = provider
                result.models.register(ModelProfile(name="default", provider=provider))

    return result
