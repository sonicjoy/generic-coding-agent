"""Built-in tools and the tool registry.

The harness ships with a batteries-included set of tools covering exploration,
search, file reading/modification, patch application, and command execution.
Additional tools are contributed by user plugins (see :mod:`gca.plugins`).
"""

from __future__ import annotations

from gca.tools.base import Tool, ToolContext, ToolError, ToolRegistry, ToolResult
from gca.tools.control import FINISH_TOOL_NAME, control_tools
from gca.tools.filesystem import filesystem_tools
from gca.tools.patch import patch_tools
from gca.tools.search import search_tools
from gca.tools.shell import shell_tools


def builtin_tools() -> list[Tool]:
    """Return a fresh instance of every built-in tool."""

    tools: list[Tool] = []
    tools.extend(filesystem_tools())
    tools.extend(search_tools())
    tools.extend(patch_tools())
    tools.extend(shell_tools())
    tools.extend(control_tools())
    return tools


def build_registry(extra: list[Tool] | None = None) -> ToolRegistry:
    """Build a registry pre-populated with built-in tools plus any extras."""

    registry = ToolRegistry()
    for tool in builtin_tools():
        registry.register(tool)
    for tool in extra or []:
        registry.register(tool)
    return registry


__all__ = [
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "FINISH_TOOL_NAME",
    "builtin_tools",
    "build_registry",
]
