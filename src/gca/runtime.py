"""Assembly layer: wire providers, tools, skills, context, and sessions together."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from gca.agent import Agent, AgentConfig, EventHook
from gca.context import build_context_prompt
from gca.credentials import CredentialBroker
from gca.executor.docker import DockerExecutor
from gca.executor.protocol import CommandExecutor
from gca.models import ModelRegistry
from gca.orchestrator import RunCoordinator
from gca.personas import PersonaSet, load_personas
from gca.plugins import LoadedPlugins, load_plugins
from gca.providers.base import LLMProvider, Message
from gca.repo_config import RepoConfig, load_repo_config
from gca.session import Session, SessionStore
from gca.skills import LoadSkillTool, SkillRegistry
from gca.tool_policy import (
    register_fixed_commands,
    tool_names_for_phase,
    validate_all_phase_policies,
)
from gca.tools import build_registry
from gca.tools.base import ExecutionPolicy, ToolContext, ToolRegistry
from gca.workspace.layout import normalize_run_id

DEFAULT_SYSTEM_PROMPT = """\
You are a generic coding agent operating autonomously inside a project workspace.

I will produce code that is correct, safe, readable, and aligned with the
repository's architecture.

I will reason explicitly, plan carefully, implement minimally, and review
rigorously.

I will never modify forbidden paths, never publish failing work, and never
exceed my authority.

I will behave like a senior engineer, not a code generator.

Operating procedure:
- Reason step by step and maintain a plan; update it as you learn.
- Use tools to gather context before acting: explore the structure, search, and
  read files rather than guessing.
- Prefer small, targeted edits. When modifying existing files, generate a unified
  diff and use 'apply_patch', or 'search_replace' for a single exact string change.
  Avoid rewriting whole large files with 'write_file'.
- After making changes, run the project's tests, linters, and build via
  'run_command'. If something fails, read the output and fix it, then retry.
- Only change what the task requires. Keep edits minimal and safe.
- When the task is fully complete and verified, call the 'finish' tool with a
  short summary. Do not call 'finish' prematurely.
"""


@dataclass
class RuntimeConfig:
    workspace: Path
    sessions_dir: Path
    plugins_dir: Path | None = None
    skill_dirs: list[Path] | None = None
    max_steps: int = 25
    workflow: str | None = None
    models_paths: list[Path] | None = None
    repo_config: RepoConfig | None = None
    trusted_model_paths_only: bool = False
    executor: CommandExecutor | None = None
    prepare_executor: bool = True


def default_skill_dirs(workspace: Path) -> list[Path]:
    return [workspace / ".gca" / "skills", workspace / "skills"]


def build_system_prompt(
    workspace: Path,
    skills: SkillRegistry,
    repo_config: RepoConfig | None = None,
    personas: PersonaSet | None = None,
) -> str:
    """Build the base prompt from persona, project instructions, and skill catalog."""

    resolved = repo_config or load_repo_config(workspace)
    persona_set = personas or load_personas(
        resolved.context.persona_file,
        resolved.context.phase_personas,
    )
    parts = [persona_set.base or DEFAULT_SYSTEM_PROMPT]
    context = build_context_prompt(
        workspace,
        filenames=resolved.context.files,
        include_frontmatter=resolved.context.include_frontmatter,
    )
    if context:
        parts.append("Project instructions:\n" + context)
    catalog = skills.catalog()
    if catalog:
        parts.append(catalog)
    return "\n\n".join(parts)


def build_registry_with_extras(
    config: RuntimeConfig,
    skills: SkillRegistry,
    loaded_plugins: LoadedPlugins | None = None,
) -> ToolRegistry:
    """Build the tool registry without reloading already-loaded plugins."""

    repo_config = config.repo_config or load_repo_config(config.workspace)
    registry = build_registry(hosted=repo_config.runtime.profile == "hosted")
    registry.register(LoadSkillTool(skills))
    if loaded_plugins is not None:
        loaded_plugins.register_tools(registry)
    else:
        repo_config = config.repo_config or load_repo_config(config.workspace)
        plugins_dir = config.plugins_dir or repo_config.plugins.directory
        if plugins_dir is not None:
            load_configured_plugins(config).register_tools(registry)
    repo_config = config.repo_config or load_repo_config(config.workspace)
    register_fixed_commands(registry, repo_config)
    validate_all_phase_policies(registry, repo_config)
    return registry


def load_configured_plugins(config: RuntimeConfig) -> LoadedPlugins:
    """Load plugins after applying repository and hosted-mode trust policy."""

    repo_config = config.repo_config or load_repo_config(config.workspace)
    plugins_dir = config.plugins_dir or repo_config.plugins.directory
    if plugins_dir is None:
        return LoadedPlugins()
    _validate_plugin_directory(config.workspace, plugins_dir, repo_config)
    allowed = set(repo_config.plugins.allow) or None
    return load_plugins(plugins_dir, allowed_modules=allowed)


def create_agent(
    config: RuntimeConfig,
    provider: LLMProvider,
    session: Session,
    store: SessionStore,
    on_event: EventHook | None = None,
) -> Agent:
    """Build a fully-wired :class:`Agent` for the given session."""

    repo_config = config.repo_config or load_repo_config(config.workspace)
    skill_dirs = (
        config.skill_dirs or list(repo_config.skill_dirs) or default_skill_dirs(config.workspace)
    )
    skills = SkillRegistry.discover(skill_dirs)
    registry = build_registry_with_extras(config, skills)

    if not session.messages:
        system_prompt = build_system_prompt(config.workspace, skills, repo_config)
        session.messages.append(Message(role="system", content=system_prompt))
        session.messages.append(Message(role="user", content=session.task))

    allowed = tool_names_for_phase(registry, repo_config, "execute", workflow="fast")
    credentials = CredentialBroker.from_environment(
        include_names=_configured_secret_names(repo_config)
    )
    executor = _ensure_executor(config, repo_config, session.id)
    context = ToolContext(
        workspace=config.workspace,
        allowed_tools=allowed,
        tool_secret_access=repo_config.tools.secret_access,
        execution=_execution_policy(repo_config),
        credentials=credentials,
        executor=executor,
    )
    return Agent(
        provider=provider,
        registry=registry,
        session=session,
        context=context,
        store=store,
        config=AgentConfig(max_steps=config.max_steps),
        on_event=on_event,
    )


def create_coordinator(
    config: RuntimeConfig,
    models: ModelRegistry,
    *,
    loaded_plugins: LoadedPlugins | None = None,
    on_event: EventHook | None = None,
) -> RunCoordinator:
    """Build the workflow coordinator used by the CLI."""

    if len(models) == 0:
        raise ValueError("at least one model must be registered")
    repo_config = config.repo_config or load_repo_config(config.workspace)
    skill_dirs = (
        config.skill_dirs or list(repo_config.skill_dirs) or default_skill_dirs(config.workspace)
    )
    skills = SkillRegistry.discover(skill_dirs)
    registry = build_registry_with_extras(config, skills, loaded_plugins)
    personas = load_personas(repo_config.context.persona_file, repo_config.context.phase_personas)
    executor = _ensure_executor(config, repo_config, "coordinator")
    return RunCoordinator(
        workspace=config.workspace,
        max_steps=config.max_steps,
        requested_workflow=config.workflow,
        models=models,
        policy=repo_config.routing,
        tools=registry,
        system_prompt=build_system_prompt(config.workspace, skills, repo_config, personas),
        personas=personas,
        config_fingerprint=repo_config.fingerprint(),
        repo_config=repo_config,
        execution_policy=_execution_policy(repo_config),
        credentials=CredentialBroker.from_environment(
            include_names=_configured_secret_names(repo_config)
        ),
        on_event=on_event,
        executor=executor,
    )


def _ensure_executor(
    config: RuntimeConfig,
    repo_config: RepoConfig,
    run_id: str,
) -> CommandExecutor | None:
    if config.executor is not None:
        return config.executor
    if not config.prepare_executor:
        return None
    try:
        identity = normalize_run_id(run_id)
    except ValueError:
        identity = uuid4().hex
    executor = DockerExecutor.create(
        config.workspace,
        repo_config.environment,
        run_id=identity,
    )
    if isinstance(executor, DockerExecutor):
        executor.build()
    return executor


def _execution_policy(config: RepoConfig) -> ExecutionPolicy:
    return ExecutionPolicy(
        profile=config.runtime.profile,
        max_tool_timeout=config.runtime.max_tool_timeout,
        max_output_chars=config.runtime.max_output_chars,
        max_read_bytes=config.runtime.max_read_bytes,
    )


def _configured_secret_names(config: RepoConfig) -> frozenset[str]:
    return frozenset(
        name for secret_names in config.tools.secret_access.values() for name in secret_names
    )


def _validate_plugin_directory(workspace: Path, directory: Path, config: RepoConfig) -> None:
    if config.runtime.profile != "hosted":
        return
    root = workspace.resolve()
    target = directory.resolve()
    if target == root or root in target.parents:
        raise ValueError(
            "hosted mode refuses plugins from the repository checkout; "
            "use an operator-installed --plugins directory"
        )
