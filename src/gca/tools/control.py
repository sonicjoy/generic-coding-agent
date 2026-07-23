"""Control tools that steer the agent loop itself."""

from __future__ import annotations

from typing import Any

from gca.tools.base import Tool, ToolContext, ToolResult

FINISH_TOOL_NAME = "finish"


class FinishTool(Tool):
    """Signal that the task is complete. Calling this stops the agent loop."""

    name = FINISH_TOOL_NAME
    capabilities = frozenset({"control"})
    description = (
        "Call this when the task is fully complete. Provide a short summary of what "
        "was done. Invoking this ends the session successfully."
    )
    parameters = {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Summary of the completed work."}
        },
        "required": ["summary"],
    }

    def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        return ToolResult.success(str(kwargs.get("summary", "Task complete.")))


def control_tools() -> list[Tool]:
    return [FinishTool()]
