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
