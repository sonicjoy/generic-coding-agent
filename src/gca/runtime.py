"""Assembly layer: wire providers, tools, skills, context, and sessions together."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gca.agent import Agent, AgentConfig, EventHook
from gca.context import build_context_prompt
from gca.models import ModelRegistry
from gca.orchestrator import RunCoordinator
from gca.personas import PersonaSet, load_personas
from gca.plugins import LoadedPlugins, load_plugins
from gca.providers.base import LLMProvider, Message
from gca.repo_config import RepoConfig, load_repo_config
from gca.session import Session, SessionStore
from gca.skills import LoadSkillTool, SkillRegistry
from gca.tools import build_registry
from gca.tools.base import ToolContext, ToolRegistry

DEFAULT_SYSTEM_PROMPT = """\
You are a generic coding agent operating autonomously inside a project workspace.

Operating procedure:
- Reason step by step and maintain a plan; update it as you learn.
- Use tools to gather context before acting: explore the structure, search, and
  read files rather than guessing.
- Prefer small, targeted edits. When modifying existing files, generate a unified
  diff and use the 'apply_patch' tool instead of rewriting whole files.
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

    registry = build_registry()
    registry.register(LoadSkillTool(skills))
    if loaded_plugins is not None:
        loaded_plugins.register_tools(registry)
    else:
        repo_config = config.repo_config or load_repo_config(config.workspace)
        plugins_dir = config.plugins_dir or repo_config.plugins.directory
        if plugins_dir is not None:
            load_plugins(plugins_dir, registry)
    return registry


def resolve_provider(config: RuntimeConfig, fallback: LLMProvider) -> LLMProvider:
    """Use a provider supplied by a plugin if present, else the fallback."""

    repo_config = config.repo_config or load_repo_config(config.workspace)
    plugins_dir = config.plugins_dir or repo_config.plugins.directory
    if plugins_dir is not None:
        loaded = load_plugins(plugins_dir)
        if loaded.provider is not None:
            return loaded.provider
    return fallback


def create_agent(
    config: RuntimeConfig,
    provider: LLMProvider,
    session: Session,
    store: SessionStore,
    on_event: EventHook | None = None,
) -> Agent:
    """Build a fully-wired :class:`Agent` for the given session."""

    repo_config = config.repo_config or load_repo_config(config.workspace)
    skill_dirs = config.skill_dirs or list(repo_config.skill_dirs) or default_skill_dirs(config.workspace)
    skills = SkillRegistry.discover(skill_dirs)
    registry = build_registry_with_extras(config, skills)

    if not session.messages:
        system_prompt = build_system_prompt(config.workspace, skills, repo_config)
        session.messages.append(Message(role="system", content=system_prompt))
        session.messages.append(Message(role="user", content=session.task))

    context = ToolContext(workspace=config.workspace)
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
    skill_dirs = config.skill_dirs or list(repo_config.skill_dirs) or default_skill_dirs(config.workspace)
    skills = SkillRegistry.discover(skill_dirs)
    registry = build_registry_with_extras(config, skills, loaded_plugins)
    personas = load_personas(repo_config.context.persona_file, repo_config.context.phase_personas)
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
        on_event=on_event,
    )
