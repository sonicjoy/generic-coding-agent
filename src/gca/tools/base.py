"""Tool contract and registry.

A tool is a typed capability the agent can invoke. Tools declare a JSON-schema
``parameters`` block (advertised to the model) and implement :meth:`Tool.run`,
which receives a :class:`ToolContext` (workspace sandbox root) and keyword
arguments matching the schema.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gca.providers.base import ToolSpec


class ToolError(Exception):
    """Raised when a tool cannot complete a request (reported back to the model)."""


@dataclass
class ToolContext:
    """Execution context shared with every tool.

    ``workspace`` is the sandbox root. All filesystem tools resolve and confine
    paths beneath it so the agent cannot read or modify files outside the project.
    """

    workspace: Path

    def resolve(self, relative: str) -> Path:
        """Resolve ``relative`` under the workspace, rejecting path escapes."""

        target = (self.workspace / relative).resolve()
        root = self.workspace.resolve()
        if target != root and root not in target.parents:
            raise ToolError(f"path escapes workspace: {relative!r}")
        return target


@dataclass
class ToolResult:
    """Outcome of a tool invocation."""

    ok: bool
    output: str

    @classmethod
    def success(cls, output: str) -> ToolResult:
        return cls(ok=True, output=output)

    @classmethod
    def failure(cls, output: str) -> ToolResult:
        return cls(ok=False, output=output)


class Tool(ABC):
    """Base class for all tools."""

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters)

    @abstractmethod
    def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        raise NotImplementedError


class ToolRegistry:
    """A name-indexed collection of tools."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError(f"tool {tool!r} has no name")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [self._tools[name].spec() for name in self.names()]

    def subset(self, names: set[str]) -> ToolRegistry:
        """Return a registry containing only the requested existing tools."""

        registry = ToolRegistry()
        for name in sorted(names):
            tool = self.get(name)
            if tool is not None:
                registry.register(tool)
        return registry

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools
