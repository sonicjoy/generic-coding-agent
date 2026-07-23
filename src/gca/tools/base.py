"""Tool contract and registry.

A tool is a typed capability the agent can invoke. Tools declare a JSON-schema
``parameters`` block (advertised to the model) and implement :meth:`Tool.run`,
which receives a :class:`ToolContext` (workspace sandbox root) and keyword
arguments matching the schema.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from gca.credentials import CredentialBroker
from gca.executor.protocol import CommandExecutor
from gca.providers.base import ToolSpec


class ToolError(Exception):
    """Raised when a tool cannot complete a request (reported back to the model)."""


@dataclass
class ExecutionPolicy:
    """Hard runtime limits shared by all tools."""

    profile: str = "local"
    max_tool_timeout: int = 300
    max_output_chars: int = 20_000
    max_read_bytes: int = 1_000_000


@dataclass
class ToolContext:
    """Execution context shared with every tool.

    ``workspace`` is the sandbox root. All filesystem tools resolve and confine
    paths beneath it so the agent cannot read or modify files outside the project.
    """

    workspace: Path
    phase: str = "execute"
    audit_id: str = ""
    allowed_tools: frozenset[str] | None = None
    allowed_secrets: frozenset[str] = frozenset()
    tool_secret_access: dict[str, frozenset[str]] = field(default_factory=dict)
    current_tool: str = ""
    execution: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    credentials: CredentialBroker = field(default_factory=CredentialBroker.from_environment)
    executor: CommandExecutor | None = None

    def resolve(self, relative: str) -> Path:
        """Resolve ``relative`` under the workspace, rejecting path escapes."""

        requested = Path(relative)
        target = (self.workspace / requested).resolve()
        root = self.workspace.resolve()
        if target != root and root not in target.parents:
            raise ToolError(f"path escapes workspace: {relative!r}")
        if _is_protected_path(requested.parts) or (
            target != root and _is_protected_path(target.relative_to(root).parts)
        ):
            raise ToolError(f"path is protected from agent tools: {relative!r}")
        if self.execution.profile == "hosted" and target != root:
            normalized = target.relative_to(root).parts
            if normalized == (".gca", "config.yaml"):
                raise ToolError("hosted repository policy is immutable during a run")
        return target

    def allows(self, tool_name: str) -> bool:
        """Return whether the current phase permits ``tool_name``."""

        return self.allowed_tools is None or tool_name in self.allowed_tools

    def secret(self, name: str) -> str:
        """Return an authorized secret without exposing the broker directly."""

        try:
            return self.credentials.get(name, allowed=self.allowed_secrets)
        except (KeyError, PermissionError) as exc:
            raise ToolError(str(exc)) from exc

    def for_tool(self, name: str) -> ToolContext:
        """Return a context narrowed to one tool's declared secret access."""

        return replace(
            self,
            current_tool=name,
            allowed_secrets=self.tool_secret_access.get(name, frozenset()),
        )

    def subprocess_env(self, *, allowed_keys: frozenset[str] = frozenset()) -> dict[str, str]:
        """Return a credential-sanitized subprocess environment."""

        return self.credentials.subprocess_env(
            self.execution.profile,
            allowed_keys=allowed_keys,
        )

    def redact(self, text: str) -> str:
        """Redact known secrets from model-facing or persisted output."""

        return self.credentials.redact(text)


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
    capabilities: frozenset[str] = frozenset()
    risk: str = "low"

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


def _is_protected_path(parts: tuple[str, ...]) -> bool:
    normalized = tuple(part for part in parts if part not in {"", "."})
    if ".git" in normalized:
        return True
    if len(normalized) >= 2 and normalized[:2] in {
        (".gca", "sessions"),
        (".gca", "jobs"),
    }:
        return True
    return any(
        part == ".env" or (part.startswith(".env.") and part != ".env.example")
        for part in normalized
    )
