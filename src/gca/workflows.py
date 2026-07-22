"""Built-in workflow definitions and phase-completion tools."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from gca.tools.base import Tool, ToolContext, ToolResult
from gca.tools.control import FINISH_TOOL_NAME

READ_TOOLS = frozenset({"explore", "load_skill", "read_file", "search"})
REVIEW_TOOLS = READ_TOOLS | {"run_command"}


@dataclass(frozen=True)
class PhaseSpec:
    """One role-bound agent phase in a workflow."""

    name: str
    capability: str
    model_role: str
    strategy: str
    allowed_tools: frozenset[str] | None = None


@dataclass(frozen=True)
class WorkflowSpec:
    """An ordered collection of agent phases."""

    name: str
    phases: tuple[PhaseSpec, ...]


FAST_WORKFLOW = WorkflowSpec(
    name="fast",
    phases=(
        PhaseSpec(
            name="execute",
            capability="coding",
            model_role="fast",
            strategy="efficient",
        ),
    ),
)

FEATURE_WORKFLOW = WorkflowSpec(
    name="feature",
    phases=(
        PhaseSpec(
            name="planning",
            capability="planning",
            model_role="planning",
            strategy="strongest",
            allowed_tools=READ_TOOLS,
        ),
        PhaseSpec(
            name="implementation",
            capability="coding",
            model_role="implementation",
            strategy="efficient",
        ),
        PhaseSpec(
            name="review",
            capability="review",
            model_role="review",
            strategy="strongest",
            allowed_tools=REVIEW_TOOLS,
        ),
    ),
)

BUILTIN_WORKFLOWS = {
    FAST_WORKFLOW.name: FAST_WORKFLOW,
    FEATURE_WORKFLOW.name: FEATURE_WORKFLOW,
}


def get_workflow(name: str) -> WorkflowSpec:
    """Return a built-in workflow by name."""

    try:
        return BUILTIN_WORKFLOWS[name]
    except KeyError as exc:
        raise ValueError(f"unknown workflow: {name}") from exc


class SubmitPlanTool(Tool):
    """Complete planning with a structured implementation plan."""

    name = FINISH_TOOL_NAME
    description = (
        "Complete the planning phase. Supply a detailed implementation and verification "
        "plan for the implementation agent."
    )
    parameters = {
        "type": "object",
        "properties": {
            "plan": {
                "type": "string",
                "description": "Detailed implementation and verification plan.",
            }
        },
        "required": ["plan"],
    }

    def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        return ToolResult.success(str(kwargs.get("plan", "")))


class SubmitReviewTool(Tool):
    """Complete review with an approval or requested changes."""

    name = FINISH_TOOL_NAME
    description = (
        "Complete the review phase with a structured verdict. Approve only after "
        "inspecting the implementation and running relevant checks."
    )
    parameters = {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["approved", "changes_requested"],
            },
            "summary": {
                "type": "string",
                "description": "Review evidence, or specific changes required.",
            },
        },
        "required": ["verdict", "summary"],
    }

    def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        verdict = str(kwargs.get("verdict", "changes_requested"))
        summary = str(kwargs.get("summary", ""))
        return ToolResult.success(json.dumps({"verdict": verdict, "summary": summary}))
