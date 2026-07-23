"""Control tools that steer the agent loop itself."""

from __future__ import annotations

from typing import Any

from gca.tools.base import Tool, ToolContext, ToolResult

FINISH_TOOL_NAME = "finish"
CHANGES_READY_TOOL_NAME = "changes_ready"
NEEDS_HUMAN_TOOL_NAME = "needs_human"
NO_SAFE_CHANGE_TOOL_NAME = "no_safe_change"
FAIL_TURN_TOOL_NAME = "fail_turn"

HOSTED_CONTROL_TOOL_NAMES = frozenset(
    {
        FINISH_TOOL_NAME,
        CHANGES_READY_TOOL_NAME,
        NEEDS_HUMAN_TOOL_NAME,
        NO_SAFE_CHANGE_TOOL_NAME,
        FAIL_TURN_TOOL_NAME,
    }
)


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


class ChangesReadyTool(Tool):
    """Hosted control tool: code changes are ready for service-owned publication."""

    name = CHANGES_READY_TOOL_NAME
    capabilities = frozenset({"control"})
    description = (
        "Call this when code changes in the workspace are ready for the service to "
        "commit, push, and open or update a merge request. Provide a short summary."
    )
    parameters = {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Summary of the ready changes.",
            }
        },
        "required": ["summary"],
    }

    def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        return ToolResult.success(str(kwargs.get("summary", "Changes ready.")))


class NeedsHumanTool(Tool):
    """Hosted control tool: ask a clarifying question and wait."""

    name = NEEDS_HUMAN_TOOL_NAME
    capabilities = frozenset({"control"})
    description = (
        "Call this when more human context is required before coding. Provide one "
        "clear question. The service will post it on the issue and pause the session."
    )
    parameters = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "Clarifying question for the human.",
            }
        },
        "required": ["question"],
    }

    def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        return ToolResult.success(str(kwargs.get("question", "Need more information.")))


class NoSafeChangeTool(Tool):
    """Hosted control tool: report that no safe change should be made."""

    name = NO_SAFE_CHANGE_TOOL_NAME
    capabilities = frozenset({"control"})
    description = (
        "Call this when the issue should not be changed yet, with a reason and brief evidence."
    )
    parameters = {
        "type": "object",
        "properties": {
            "reason": {"type": "string"},
            "evidence": {"type": "string"},
        },
        "required": ["reason"],
    }

    def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        reason = str(kwargs.get("reason", "No safe change"))
        evidence = str(kwargs.get("evidence", "")).strip()
        return ToolResult.success(reason if not evidence else f"{reason}\nEvidence: {evidence}")


class FailTurnTool(Tool):
    """Hosted control tool: fail the current turn with a reason."""

    name = FAIL_TURN_TOOL_NAME
    capabilities = frozenset({"control"})
    description = "Call this when the turn cannot continue and should fail."
    parameters = {
        "type": "object",
        "properties": {"reason": {"type": "string"}},
        "required": ["reason"],
    }

    def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        return ToolResult.success(str(kwargs.get("reason", "Turn failed.")))


def control_tools(*, hosted: bool = False) -> list[Tool]:
    """Return control tools for local or hosted agent loops."""

    tools: list[Tool] = [FinishTool()]
    if hosted:
        tools.extend(
            [
                ChangesReadyTool(),
                NeedsHumanTool(),
                NoSafeChangeTool(),
                FailTurnTool(),
            ]
        )
    return tools
