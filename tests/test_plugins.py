from __future__ import annotations

from pathlib import Path

from gca.plugins import load_plugins
from gca.tools.base import ToolContext, ToolRegistry

_PLUGIN_SRC = """
from gca.tools.base import Tool, ToolResult


class ShoutTool(Tool):
    name = "shout"
    description = "Uppercase a message."
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def run(self, ctx, **kwargs):
        return ToolResult.success(str(kwargs["text"]).upper())


TOOLS = [ShoutTool()]
"""

_MODELS_PLUGIN_SRC = """
from gca.models import ModelProfile
from gca.providers.base import LLMProvider, LLMResponse


class Provider(LLMProvider):
    def complete(self, messages, tools):
        return LLMResponse(content="done")


def get_models():
    return [
        ModelProfile("fast", Provider(), strength=2, speed=5, cost=1),
        ModelProfile("strong", Provider(), strength=5, speed=2, cost=5),
    ]
"""

_LEGACY_PROVIDER_PLUGIN_SRC = """
from gca.providers.base import LLMProvider, LLMResponse


class Provider(LLMProvider):
    def complete(self, messages, tools):
        return LLMResponse(content="done")


def get_provider():
    return Provider()
"""


def test_load_tool_plugin(tmp_path: Path) -> None:
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "shout.py").write_text(_PLUGIN_SRC, encoding="utf-8")

    registry = ToolRegistry()
    loaded = load_plugins(plugins_dir, registry)
    assert "shout.py" in loaded.modules
    assert "shout" in registry
    tool = registry.get("shout")
    assert tool is not None
    result = tool.run(ToolContext(workspace=tmp_path), text="hi")
    assert result.output == "HI"


def test_load_multiple_model_profiles(tmp_path: Path) -> None:
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "models.py").write_text(_MODELS_PLUGIN_SRC, encoding="utf-8")

    loaded = load_plugins(plugins_dir)

    assert loaded.models.names() == ["fast", "strong"]
    strong = loaded.models.get("strong")
    assert strong is not None
    assert strong.strength == 5


def test_legacy_provider_is_registered_as_default(tmp_path: Path) -> None:
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "legacy.py").write_text(
        _LEGACY_PROVIDER_PLUGIN_SRC,
        encoding="utf-8",
    )

    loaded = load_plugins(plugins_dir)

    assert loaded.provider is not None
    assert loaded.models.names() == ["default"]
