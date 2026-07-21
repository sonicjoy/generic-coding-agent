"""Example user plugin: a simple word-count tool.

Demonstrates the plugin contract. Dropping this file into a plugin directory and
passing ``--plugins <dir>`` makes the ``word_count`` tool available to the agent.
No build step is required.
"""

from __future__ import annotations

from typing import Any

from gca.tools.base import Tool, ToolContext, ToolResult


class WordCountTool(Tool):
    name = "word_count"
    description = "Count the words in a workspace file."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "File to count words in."}},
        "required": ["path"],
    }

    def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        target = ctx.resolve(str(kwargs["path"]))
        if not target.is_file():
            return ToolResult.failure(f"file not found: {kwargs['path']}")
        count = len(target.read_text(encoding="utf-8").split())
        return ToolResult.success(str(count))


TOOLS = [WordCountTool()]
