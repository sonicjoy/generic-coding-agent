"""User plugin loading.

Users extend the harness by dropping Python modules into a plugin directory.
A plugin module may expose either:

* a module-level ``TOOLS`` list of :class:`~gca.tools.base.Tool` instances, and/or
* a ``register(registry)`` function that adds tools to the given registry, and/or
* a ``get_provider()`` function returning an :class:`~gca.providers.base.LLMProvider`.

Because loading is dynamic (``importlib``), no build step is required — this is
the primary extension path for provider integrations, custom tools, and skills.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gca.providers.base import LLMProvider
from gca.tools.base import Tool, ToolRegistry


@dataclass
class LoadedPlugins:
    tools: list[Tool] = field(default_factory=list)
    provider: LLMProvider | None = None
    modules: list[str] = field(default_factory=list)


def _load_module(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(f"gca_plugin_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load plugin: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_plugins(directory: Path, registry: ToolRegistry | None = None) -> LoadedPlugins:
    """Load all ``*.py`` plugins from ``directory``.

    Tools discovered via ``TOOLS`` are returned and, if a registry is provided,
    registered into it. The last plugin that defines ``get_provider`` wins.
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
        if callable(register_fn) and registry is not None:
            register_fn(registry)

        provider_fn = getattr(module, "get_provider", None)
        if callable(provider_fn):
            provider = provider_fn()
            if isinstance(provider, LLMProvider):
                result.provider = provider

    return result
