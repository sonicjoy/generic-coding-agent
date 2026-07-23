"""Versioned repository configuration for portable agent integrations."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from gca.context import discover_context_files
from gca.frontmatter import FrontmatterError, split_frontmatter
from gca.paths import WorkspacePathError, resolve_workspace_path
from gca.routing import RoutingPolicy

CONFIG_VERSION = 1
CONTEXT_FILENAMES = ("AGENTS.md", "CLAUDE.md")
PHASE_NAMES = frozenset({"execute", "planning", "implementation", "review"})
PERSONA_ROLES = frozenset({"fast", "planning", "implementation", "review"})


class RepoConfigError(ValueError):
    """Raised when repository configuration is invalid."""


@dataclass(frozen=True)
class CommandParameterConfig:
    """One bounded argument accepted by a fixed command tool."""

    type: str = "string"
    flag: str | None = None
    choices: tuple[str, ...] = ()
    required: bool = False


@dataclass(frozen=True)
class FixedCommandConfig:
    """Declarative fixed command exposed as a tool."""

    name: str
    description: str
    argv: tuple[str, ...]
    cwd: Path
    timeout: int = 120
    phases: frozenset[str] = field(
        default_factory=lambda: frozenset({"implementation", "review", "execute"})
    )
    parameters: dict[str, CommandParameterConfig] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolPolicyConfig:
    """Repository-level tool exposure and fixed command declarations."""

    deny: frozenset[str] = frozenset()
    phases: dict[str, frozenset[str]] = field(default_factory=dict)
    fixed_commands: dict[str, FixedCommandConfig] = field(default_factory=dict)
    secret_access: dict[str, frozenset[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class PluginConfig:
    """Plugin discovery policy."""

    directory: Path | None = None
    allow: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeSettings:
    """Run defaults and execution profile."""

    profile: str = "local"
    max_steps: int = 25
    max_tool_timeout: int = 300
    max_output_chars: int = 20_000
    max_read_bytes: int = 1_000_000


@dataclass(frozen=True)
class ContextSettings:
    """Project context and persona locations."""

    files: tuple[str, ...] = CONTEXT_FILENAMES
    include_frontmatter: bool = False
    persona_file: Path | None = None
    phase_personas: dict[str, Path] = field(default_factory=dict)


@dataclass(frozen=True)
class RepoConfig:
    """Fully resolved repository configuration."""

    workspace: Path
    version: int = CONFIG_VERSION
    context: ContextSettings = field(default_factory=ContextSettings)
    skill_dirs: tuple[Path, ...] = ()
    plugins: PluginConfig = field(default_factory=PluginConfig)
    tools: ToolPolicyConfig = field(default_factory=ToolPolicyConfig)
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)
    routing: RoutingPolicy = field(default_factory=RoutingPolicy)
    model_paths: tuple[Path, ...] = ()
    publication: dict[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        """Return a stable hash used for resume diagnostics."""

        payload = {
            "version": self.version,
            "context": {
                "files": self.context.files,
                "include_frontmatter": self.context.include_frontmatter,
                "persona_file": _relative(self.workspace, self.context.persona_file),
                "phase_personas": {
                    name: _relative(self.workspace, path)
                    for name, path in sorted(self.context.phase_personas.items())
                },
            },
            "skill_dirs": [_relative(self.workspace, path) for path in self.skill_dirs],
            "plugins": {
                "directory": _relative(self.workspace, self.plugins.directory),
                "allow": self.plugins.allow,
            },
            "tools": {
                "deny": sorted(self.tools.deny),
                "phases": {
                    phase: sorted(names) for phase, names in sorted(self.tools.phases.items())
                },
                "fixed_commands": {
                    name: {
                        "argv": command.argv,
                        "cwd": _relative(self.workspace, command.cwd),
                        "timeout": command.timeout,
                        "phases": sorted(command.phases),
                        "parameters": {
                            parameter: {
                                "type": value.type,
                                "flag": value.flag,
                                "choices": value.choices,
                                "required": value.required,
                            }
                            for parameter, value in sorted(command.parameters.items())
                        },
                    }
                    for name, command in sorted(self.tools.fixed_commands.items())
                },
                "secret_access": {
                    name: sorted(secrets)
                    for name, secrets in sorted(self.tools.secret_access.items())
                },
            },
            "runtime": self.runtime.__dict__,
            "routing": self.routing.fingerprint(),
            "model_paths": [_relative(self.workspace, path) for path in self.model_paths],
            "publication": self.publication,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        return hashlib.sha256(encoded).hexdigest()


def default_repo_config_paths(workspace: Path) -> list[Path]:
    """Return user and repository manifest paths in precedence order."""

    return [Path.home() / ".gca" / "config.yaml", Path(workspace).resolve() / ".gca/config.yaml"]


def load_repo_config(workspace: Path, paths: list[Path] | None = None) -> RepoConfig:
    """Load, merge, resolve, and validate repository configuration."""

    root = Path(workspace).resolve()
    merged: dict[str, Any] = {}
    config_paths = default_repo_config_paths(root) if paths is None else paths
    for path in config_paths:
        raw = _load_file(Path(path))
        merged = _deep_merge(merged, raw)

    context_raw = _mapping(merged.get("context", {}), "context")
    filenames = _context_filenames(context_raw.get("files", list(CONTEXT_FILENAMES)))
    routing_raw = _mapping(merged.get("routing", {}), "routing")
    publication_raw = _mapping(merged.get("publication", {}), "publication")
    for context_file in discover_context_files(root, filenames=filenames):
        try:
            metadata, _ = split_frontmatter(context_file.content, source=context_file.path)
        except FrontmatterError as exc:
            raise RepoConfigError(str(exc)) from exc
        gca = metadata.get("gca")
        if gca is None:
            continue
        if not isinstance(gca, Mapping):
            raise RepoConfigError(f"'gca' frontmatter in {context_file.path} must be a mapping")
        gca_data = dict(gca)
        publication = gca_data.pop("publication", None)
        routing_raw = _deep_merge(routing_raw, gca_data)
        # Only the repository root AGENTS.md/CLAUDE.md may contribute publication policy.
        if (
            publication is not None
            and Path(context_file.path).resolve().parent == root
            and Path(context_file.path).name in {"AGENTS.md", "CLAUDE.md"}
        ):
            if not isinstance(publication, Mapping):
                raise RepoConfigError(
                    f"'gca.publication' frontmatter in {context_file.path} must be a mapping"
                )
            publication_raw = _deep_merge(publication_raw, dict(publication))
    merged["routing"] = routing_raw
    merged["publication"] = publication_raw
    return _parse(root, merged)


def _load_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RepoConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise RepoConfigError(f"{path} must contain a mapping")
    raw = dict(value)
    if raw.get("version") != CONFIG_VERSION:
        raise RepoConfigError(f"{path} must declare version: {CONFIG_VERSION}")
    return raw


def _parse(workspace: Path, raw: dict[str, Any]) -> RepoConfig:
    allowed = {
        "version",
        "context",
        "skills",
        "plugins",
        "tools",
        "runtime",
        "routing",
        "models",
        "publication",
    }
    _reject_unknown(raw, allowed, "repository configuration")
    version = raw.get("version", CONFIG_VERSION)
    if version != CONFIG_VERSION:
        raise RepoConfigError(f"configuration version must be {CONFIG_VERSION}")

    context = _parse_context(workspace, _mapping(raw.get("context", {}), "context"))
    skills_raw = _mapping(raw.get("skills", {}), "skills")
    _reject_unknown(skills_raw, {"dirs"}, "skills")
    skill_values = _string_list(skills_raw.get("dirs", [".gca/skills", "skills"]), "skills.dirs")
    skill_dirs = tuple(_resolve(workspace, value, "skills.dirs") for value in skill_values)

    plugins = _parse_plugins(workspace, _mapping(raw.get("plugins", {}), "plugins"))
    tools = _parse_tools(workspace, _mapping(raw.get("tools", {}), "tools"))
    runtime = _parse_runtime(_mapping(raw.get("runtime", {}), "runtime"))

    models_raw = _mapping(raw.get("models", {}), "models")
    _reject_unknown(models_raw, {"paths"}, "models")
    model_paths = tuple(
        _resolve(workspace, value, "models.paths")
        for value in _string_list(models_raw.get("paths", []), "models.paths")
    )
    for path in model_paths:
        if not path.is_file():
            raise RepoConfigError(f"configured models path does not exist: {path}")

    try:
        routing = RoutingPolicy.from_mapping(_mapping(raw.get("routing", {}), "routing"))
    except ValueError as exc:
        raise RepoConfigError(str(exc)) from exc

    publication = _parse_publication(_mapping(raw.get("publication", {}), "publication"))
    return RepoConfig(
        workspace=workspace,
        version=version,
        context=context,
        skill_dirs=skill_dirs,
        plugins=plugins,
        tools=tools,
        runtime=runtime,
        routing=routing,
        model_paths=model_paths,
        publication=publication,
    )


def _parse_context(workspace: Path, raw: dict[str, Any]) -> ContextSettings:
    allowed = {"files", "include_frontmatter", "persona_file", "phase_personas"}
    _reject_unknown(raw, allowed, "context")
    files = _context_filenames(raw.get("files", list(CONTEXT_FILENAMES)))
    include = _boolean(raw.get("include_frontmatter", False), "context.include_frontmatter")
    persona_file = _optional_path(workspace, raw.get("persona_file"), "context.persona_file")
    roles_raw = _mapping(raw.get("phase_personas", {}), "context.phase_personas")
    unknown_roles = sorted(set(roles_raw) - PERSONA_ROLES)
    if unknown_roles:
        raise RepoConfigError(f"unknown phase persona roles: {', '.join(unknown_roles)}")
    phase_personas = {
        role: _required_file(
            _resolve(workspace, _nonempty_string(value, f"context.phase_personas.{role}"), role),
            f"context.phase_personas.{role}",
        )
        for role, value in roles_raw.items()
    }
    if persona_file is not None:
        persona_file = _required_file(persona_file, "context.persona_file")
    return ContextSettings(
        files=files,
        include_frontmatter=include,
        persona_file=persona_file,
        phase_personas=phase_personas,
    )


def _parse_plugins(workspace: Path, raw: dict[str, Any]) -> PluginConfig:
    _reject_unknown(raw, {"directory", "allow"}, "plugins")
    directory = _optional_path(workspace, raw.get("directory"), "plugins.directory")
    if directory is not None and not directory.is_dir():
        raise RepoConfigError(f"configured plugin directory does not exist: {directory}")
    allow = tuple(_string_list(raw.get("allow", []), "plugins.allow"))
    return PluginConfig(directory=directory, allow=allow)


def _parse_tools(workspace: Path, raw: dict[str, Any]) -> ToolPolicyConfig:
    _reject_unknown(raw, {"deny", "phases", "fixed_commands", "secret_access"}, "tools")
    deny = frozenset(_string_list(raw.get("deny", []), "tools.deny"))
    phases_raw = _mapping(raw.get("phases", {}), "tools.phases")
    unknown_phases = sorted(set(phases_raw) - PHASE_NAMES)
    if unknown_phases:
        raise RepoConfigError(f"unknown tool phases: {', '.join(unknown_phases)}")
    phases: dict[str, frozenset[str]] = {}
    for phase, value in phases_raw.items():
        if isinstance(value, Mapping):
            phase_mapping = dict(value)
            _reject_unknown(phase_mapping, {"allow"}, f"tools.phases.{phase}")
            value = phase_mapping.get("allow", [])
        phases[phase] = frozenset(_string_list(value, f"tools.phases.{phase}"))

    commands_raw = _mapping(raw.get("fixed_commands", {}), "tools.fixed_commands")
    commands = {
        name: _parse_fixed_command(workspace, name, value) for name, value in commands_raw.items()
    }
    overlap = sorted(deny & set(commands))
    if overlap:
        raise RepoConfigError(f"fixed commands are also denied: {', '.join(overlap)}")
    secrets_raw = _mapping(raw.get("secret_access", {}), "tools.secret_access")
    secret_access = {
        tool_name: frozenset(_string_list(secret_names, f"tools.secret_access.{tool_name}"))
        for tool_name, secret_names in secrets_raw.items()
    }
    for tool_name, secret_names in secret_access.items():
        invalid = sorted(
            name for name in secret_names if re.fullmatch(r"[A-Z_][A-Z0-9_]*", name) is None
        )
        if invalid:
            raise RepoConfigError(
                f"tools.secret_access.{tool_name} contains invalid environment names: "
                f"{', '.join(invalid)}"
            )
    if "run_command" in secret_access:
        raise RepoConfigError("run_command cannot receive secrets; use a fixed or integration tool")
    return ToolPolicyConfig(
        deny=deny,
        phases=phases,
        fixed_commands=commands,
        secret_access=secret_access,
    )


def _parse_fixed_command(workspace: Path, raw_name: object, value: object) -> FixedCommandConfig:
    name = _nonempty_string(raw_name, "fixed command name")
    if re.fullmatch(r"[a-z][a-z0-9_]*", name) is None:
        raise RepoConfigError(
            f"fixed command name must use lower-case letters, digits, and underscores: {name}"
        )
    raw = _mapping(value, f"tools.fixed_commands.{name}")
    allowed = {"description", "argv", "cwd", "timeout", "phases", "parameters"}
    _reject_unknown(raw, allowed, f"tools.fixed_commands.{name}")
    argv = tuple(_string_list(raw.get("argv"), f"tools.fixed_commands.{name}.argv"))
    if not argv:
        raise RepoConfigError(f"tools.fixed_commands.{name}.argv must not be empty")
    if argv[0].startswith("-"):
        raise RepoConfigError(f"tools.fixed_commands.{name}.argv starts with an invalid command")
    cwd = _resolve(workspace, str(raw.get("cwd", ".")), f"tools.fixed_commands.{name}.cwd")
    if not cwd.is_dir():
        raise RepoConfigError(f"fixed command cwd does not exist: {cwd}")
    timeout = _integer(
        raw.get("timeout", 120), f"tools.fixed_commands.{name}.timeout", minimum=1, maximum=3600
    )
    phases = frozenset(
        _string_list(
            raw.get("phases", ["execute", "implementation", "review"]),
            f"tools.fixed_commands.{name}.phases",
        )
    )
    unknown_phases = sorted(phases - PHASE_NAMES)
    if unknown_phases:
        details = ", ".join(unknown_phases)
        raise RepoConfigError(f"unknown phases for fixed command {name}: {details}")
    parameters_raw = _mapping(raw.get("parameters", {}), f"tools.fixed_commands.{name}.parameters")
    parameters = {
        parameter: _parse_command_parameter(name, parameter, parameter_raw)
        for parameter, parameter_raw in parameters_raw.items()
    }
    description = raw.get("description", f"Run the configured {name} command.")
    if not isinstance(description, str) or not description.strip():
        raise RepoConfigError(f"tools.fixed_commands.{name}.description must be a string")
    return FixedCommandConfig(
        name=name,
        description=description.strip(),
        argv=argv,
        cwd=cwd,
        timeout=timeout,
        phases=phases,
        parameters=parameters,
    )


def _parse_command_parameter(
    command: str, raw_name: object, value: object
) -> CommandParameterConfig:
    name = _nonempty_string(raw_name, f"parameter name for {command}")
    if re.fullmatch(r"[a-z][a-z0-9_]*", name) is None:
        raise RepoConfigError(f"parameter name must use lower-case letters and underscores: {name}")
    raw = _mapping(value, f"tools.fixed_commands.{command}.parameters.{name}")
    _reject_unknown(raw, {"type", "flag", "choices", "required"}, f"parameter {command}.{name}")
    kind = str(raw.get("type", "string"))
    if kind not in {"string", "integer", "boolean"}:
        raise RepoConfigError(
            f"parameter {command}.{name} type must be string, integer, or boolean"
        )
    flag_value = raw.get("flag")
    flag = (
        None
        if flag_value is None
        else _nonempty_string(flag_value, f"parameter {command}.{name}.flag")
    )
    if flag is not None and not flag.startswith("-"):
        raise RepoConfigError(f"parameter {command}.{name}.flag must start with '-'")
    choices = tuple(_string_list(raw.get("choices", []), f"parameter {command}.{name}.choices"))
    required = _boolean(raw.get("required", False), f"parameter {command}.{name}.required")
    if kind == "boolean" and flag is None:
        raise RepoConfigError(f"boolean parameter {command}.{name} requires a flag")
    if kind == "boolean" and choices:
        raise RepoConfigError(f"boolean parameter {command}.{name} cannot declare choices")
    if kind != "boolean" and not choices:
        raise RepoConfigError(f"parameter {command}.{name} requires bounded choices")
    if kind == "integer" and any(re.fullmatch(r"-?[0-9]+", choice) is None for choice in choices):
        raise RepoConfigError(f"integer parameter {command}.{name} choices must be integers")
    return CommandParameterConfig(type=kind, flag=flag, choices=choices, required=required)


def _parse_runtime(raw: dict[str, Any]) -> RuntimeSettings:
    allowed = {
        "profile",
        "max_steps",
        "max_tool_timeout",
        "max_output_chars",
        "max_read_bytes",
    }
    _reject_unknown(raw, allowed, "runtime")
    profile = str(raw.get("profile", "local"))
    if profile not in {"local", "hosted"}:
        raise RepoConfigError("runtime.profile must be local or hosted")
    return RuntimeSettings(
        profile=profile,
        max_steps=_integer(raw.get("max_steps", 25), "runtime.max_steps", minimum=1, maximum=1000),
        max_tool_timeout=_integer(
            raw.get("max_tool_timeout", 300),
            "runtime.max_tool_timeout",
            minimum=1,
            maximum=3600,
        ),
        max_output_chars=_integer(
            raw.get("max_output_chars", 20_000),
            "runtime.max_output_chars",
            minimum=100,
            maximum=10_000_000,
        ),
        max_read_bytes=_integer(
            raw.get("max_read_bytes", 1_000_000),
            "runtime.max_read_bytes",
            minimum=100,
            maximum=100_000_000,
        ),
    )


def _parse_publication(raw: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "required_checks",
        "allowed_paths",
        "denied_paths",
        "max_files",
        "max_changed_lines",
        "commit_prefix",
        "auto_merge",
    }
    _reject_unknown(raw, allowed, "publication")
    result = dict(raw)
    for name in ("required_checks", "allowed_paths", "denied_paths"):
        if name in result:
            result[name] = _string_list(result[name], f"publication.{name}")
    for name in ("max_files", "max_changed_lines"):
        if name in result:
            result[name] = _integer(
                result[name],
                f"publication.{name}",
                minimum=1,
                maximum=10_000_000,
            )
    if "commit_prefix" in result:
        result["commit_prefix"] = _nonempty_string(
            result["commit_prefix"],
            "publication.commit_prefix",
        )
    if "auto_merge" in result:
        if not isinstance(result["auto_merge"], bool):
            raise RepoConfigError("publication.auto_merge must be a boolean")
    return result


def _context_filenames(value: object) -> tuple[str, ...]:
    files = tuple(_string_list(value, "context.files"))
    if not files:
        raise RepoConfigError("context.files must not be empty")
    for filename in files:
        if Path(filename).name != filename:
            raise RepoConfigError("context.files entries must be filenames, not paths")
    return files


def _optional_path(workspace: Path, value: object, label: str) -> Path | None:
    if value is None:
        return None
    return _resolve(workspace, _nonempty_string(value, label), label)


def _resolve(workspace: Path, value: str, label: str) -> Path:
    try:
        return resolve_workspace_path(workspace, value, label=label)
    except WorkspacePathError as exc:
        raise RepoConfigError(str(exc)) from exc


def _required_file(path: Path, label: str) -> Path:
    if not path.is_file():
        raise RepoConfigError(f"{label} does not exist: {path}")
    return path


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RepoConfigError(f"{label} must be a mapping")
    return {str(key): val for key, val in value.items()}


def _string_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise RepoConfigError(f"{label} must be a list of non-empty strings")
    return [item.strip() for item in value]


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RepoConfigError(f"{label} must be a non-empty string")
    return value.strip()


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise RepoConfigError(f"{label} must be a boolean")
    return value


def _integer(value: object, label: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise RepoConfigError(f"{label} must be an integer from {minimum} to {maximum}")
    return value


def _reject_unknown(raw: Mapping[str, object], allowed: set[str], label: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise RepoConfigError(f"unknown keys in {label}: {', '.join(unknown)}")


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        previous = merged.get(key)
        if isinstance(previous, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(previous, value)
        else:
            merged[key] = value
    return merged


def _relative(workspace: Path, path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return str(path.relative_to(workspace))
    except ValueError:
        return str(path)
