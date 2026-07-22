"""Declarative, phase-aware tool exposure policy."""

from __future__ import annotations

from gca.repo_config import RepoConfig
from gca.tools.base import ToolRegistry
from gca.tools.control import FINISH_TOOL_NAME
from gca.tools.fixed import FixedCommandTool
from gca.workflows import get_workflow


class ToolPolicyError(ValueError):
    """Raised when a tool policy references unavailable or unsafe tools."""


_READ_ONLY_CAPABILITIES = frozenset({"control", "knowledge", "read_external", "read_fs"})
_REVIEW_CAPABILITIES = _READ_ONLY_CAPABILITIES | {"execute"}


def register_fixed_commands(registry: ToolRegistry, config: RepoConfig) -> None:
    """Register all fixed commands declared by the repository manifest."""

    for command in config.tools.fixed_commands.values():
        if registry.get(command.name) is not None:
            raise ToolPolicyError(f"fixed command conflicts with existing tool: {command.name}")
        registry.register(FixedCommandTool(command))


def validate_tool_policy(registry: ToolRegistry, config: RepoConfig) -> None:
    """Fail closed when policy entries reference unavailable tools."""

    available = set(registry.names())
    if FINISH_TOOL_NAME in config.tools.deny:
        raise ToolPolicyError("the finish tool cannot be denied")
    referenced = set(config.tools.deny)
    for names in config.tools.phases.values():
        referenced.update(names)
    referenced.update(config.tools.secret_access)
    unknown = sorted(referenced - available)
    if unknown:
        raise ToolPolicyError(f"tool policy references unavailable tools: {', '.join(unknown)}")


def validate_all_phase_policies(registry: ToolRegistry, config: RepoConfig) -> None:
    """Resolve every built-in phase so unsafe manifest escalation fails at startup."""

    validate_tool_policy(registry, config)
    for workflow in ("fast", "feature"):
        for phase in get_workflow(workflow).phases:
            tool_names_for_phase(
                registry,
                config,
                phase.name,
                workflow=workflow,
            )


def tool_names_for_phase(
    registry: ToolRegistry,
    config: RepoConfig,
    phase: str,
    *,
    workflow: str,
) -> frozenset[str]:
    """Resolve the hard allowlist for one workflow phase."""

    validate_tool_policy(registry, config)
    override = config.tools.phases.get(phase)
    explicitly_allows_shell = override is not None and "run_command" in override
    if override is not None:
        names = set(override)
    else:
        spec = next(item for item in get_workflow(workflow).phases if item.name == phase)
        names = set(registry.names()) if spec.allowed_tools is None else set(spec.allowed_tools)
        for command in config.tools.fixed_commands.values():
            if phase in command.phases:
                names.add(command.name)

    names -= set(config.tools.deny)
    if config.runtime.profile == "hosted" and not explicitly_allows_shell:
        names.discard("run_command")
    names.add(FINISH_TOOL_NAME)
    capability_limit = {
        "planning": _READ_ONLY_CAPABILITIES,
        "review": _REVIEW_CAPABILITIES,
    }.get(phase)
    if capability_limit is not None:
        forbidden: list[str] = []
        for name in names:
            tool = registry.get(name)
            if tool is None:
                continue
            if not tool.capabilities or not tool.capabilities <= capability_limit:
                forbidden.append(name)
        if forbidden:
            raise ToolPolicyError(
                f"phase {phase} cannot expose tools with elevated capabilities: "
                f"{', '.join(sorted(forbidden))}"
            )
    return frozenset(name for name in names if registry.get(name) is not None)


def registry_for_phase(
    registry: ToolRegistry,
    config: RepoConfig,
    phase: str,
    *,
    workflow: str,
) -> ToolRegistry:
    """Return a registry containing only tools authorized for ``phase``."""

    return registry.subset(set(tool_names_for_phase(registry, config, phase, workflow=workflow)))
