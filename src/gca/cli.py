"""Command-line interface for the generic coding agent."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from gca.model_config import (
    ModelConfigError,
    build_registry_from_catalog,
    default_model_config_paths,
    load_dotenv,
    load_model_catalog,
)
from gca.models import ModelProfile, ModelRegistry
from gca.plugins import LoadedPlugins
from gca.providers.scripted import ScriptedProvider
from gca.repo_config import RepoConfigError, load_repo_config
from gca.runtime import RuntimeConfig, create_coordinator, load_configured_plugins
from gca.session import SessionStore


def _default_sessions_dir(workspace: Path) -> Path:
    return workspace / ".gca" / "sessions"


def _build_config(args: argparse.Namespace) -> RuntimeConfig:
    workspace = Path(args.workspace).resolve()
    try:
        repo_config = load_repo_config(workspace)
    except RepoConfigError as exc:
        raise SystemExit(f"Invalid .gca/config.yaml: {exc}") from exc
    sessions_dir = (
        Path(args.sessions_dir) if args.sessions_dir else _default_sessions_dir(workspace)
    )
    plugins_dir = Path(args.plugins).resolve() if args.plugins else repo_config.plugins.directory
    skill_dirs = (
        [Path(d).resolve() for d in args.skills] if args.skills else list(repo_config.skill_dirs)
    )
    models_paths = [
        *repo_config.model_paths,
        *(Path(path).resolve() for path in (args.models or [])),
    ]
    return RuntimeConfig(
        workspace=workspace,
        sessions_dir=sessions_dir,
        plugins_dir=plugins_dir,
        skill_dirs=skill_dirs,
        max_steps=args.max_steps or repo_config.runtime.max_steps,
        workflow=args.workflow,
        models_paths=models_paths or None,
        repo_config=repo_config,
    )


def _load_dotenv_files(config: RuntimeConfig) -> None:
    load_dotenv(Path.home() / ".gca" / ".env")
    load_dotenv(config.workspace / ".env")
    load_dotenv(config.workspace / ".gca" / ".env")


def _load_models(args: argparse.Namespace, config: RuntimeConfig) -> LoadedPlugins:
    _load_dotenv_files(config)
    loaded = load_configured_plugins(config)

    catalog_paths = list(default_model_config_paths(config.workspace))
    if config.models_paths:
        catalog_paths.extend(config.models_paths)
    try:
        catalog = load_model_catalog(catalog_paths)
        catalog_models = build_registry_from_catalog(catalog)
    except ModelConfigError as exc:
        raise SystemExit(f"Invalid models.yaml: {exc}") from exc

    # YAML first; plugins override same-named models as an escape hatch.
    merged = ModelRegistry()
    merged.extend(catalog_models)
    merged.extend(loaded.models)
    loaded.models = merged

    if len(loaded.models) == 0 and args.script:
        data = json.loads(Path(args.script).read_text(encoding="utf-8"))
        provider = ScriptedProvider.from_script(data)
        loaded.models.register(
            ModelProfile(
                name="scripted",
                provider=provider,
                strength=3,
                speed=5,
                cost=1,
            )
        )
    if len(loaded.models) > 0:
        return loaded
    raise SystemExit(
        "No models configured. Add a models.yaml catalog, supply --plugins with "
        "get_models()/get_provider(), or use --script PATH for the scripted provider."
    )


def _event_printer(message: str) -> None:
    print(message, file=sys.stderr)


def _cmd_run(args: argparse.Namespace) -> int:
    config = _build_config(args)
    store = SessionStore(config.sessions_dir)
    loaded = _load_models(args, config)
    session = store.create(args.task)
    print(f"session: {session.id}", file=sys.stderr)
    coordinator = create_coordinator(
        config,
        loaded.models,
        loaded_plugins=loaded,
        on_event=_event_printer,
    )
    result = coordinator.run(session, store)
    print(f"\nstatus: {result.status} (steps: {result.steps})")
    print(result.final_message)
    return 0 if result.status == "completed" else 1


def _cmd_resume(args: argparse.Namespace) -> int:
    config = _build_config(args)
    store = SessionStore(config.sessions_dir)
    loaded = _load_models(args, config)
    session = store.load(args.session_id)
    print(f"resuming session: {session.id}", file=sys.stderr)
    coordinator = create_coordinator(
        config,
        loaded.models,
        loaded_plugins=loaded,
        on_event=_event_printer,
    )
    result = coordinator.run(session, store)
    print(f"\nstatus: {result.status} (steps: {result.steps})")
    print(result.final_message)
    return 0 if result.status == "completed" else 1


def _cmd_sessions(args: argparse.Namespace) -> int:
    config = _build_config(args)
    store = SessionStore(config.sessions_dir)
    summaries = store.list()
    if not summaries:
        print("no sessions")
        return 0
    for item in summaries:
        print(
            f"{item['id']}  {item['status']:<9}  steps={item['steps']:<3}  "
            f"{item['updated_at']}  {item['task']}"
        )
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    """Validate effective repository configuration without calling an LLM."""

    config = _build_config(args)
    loaded = _load_models(args, config)
    coordinator = create_coordinator(config, loaded.models, loaded_plugins=loaded)
    missing_models = {
        role: name
        for role, name in coordinator.policy.model_preferences.items()
        if loaded.models.get(name) is None
    }
    if missing_models:
        details = ", ".join(f"{role}={name}" for role, name in sorted(missing_models.items()))
        raise SystemExit(f"Invalid model bindings: {details}")
    repo_config = config.repo_config
    if repo_config is None:
        raise RuntimeError("repository configuration was not loaded")
    print(f"configuration valid (version {repo_config.version})")
    print(f"models: {', '.join(loaded.models.names())}")
    skill_paths = [str(path) for path in config.skill_dirs or []]
    print(f"skills: {', '.join(skill_paths)}")
    print(f"tools: {', '.join(coordinator.tools.names())}")
    print(f"profile: {repo_config.runtime.profile}")
    return 0


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", default=".", help="Workspace root (default: cwd).")
    parser.add_argument("--sessions-dir", default=None, help="Where to store sessions.")
    parser.add_argument("--plugins", default=None, help="Directory of plugin modules.")
    parser.add_argument(
        "--models",
        action="append",
        default=None,
        help="Extra models.yaml path (repeatable; later files override earlier ones).",
    )
    parser.add_argument(
        "--skills", action="append", default=None, help="Skill directory (repeatable)."
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help="Max agent steps (default: repository config or 25).",
    )
    parser.add_argument(
        "--workflow",
        choices=["auto", "fast", "feature"],
        default=None,
        help="Override workflow selection (default: AGENTS.md or auto).",
    )
    parser.add_argument("--script", default=None, help="JSON script for the scripted provider.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gca", description="Generic coding agent harness.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run the agent on a new task.")
    run.add_argument("task", help="Task description for the agent.")
    _add_common(run)
    run.set_defaults(func=_cmd_run)

    resume = sub.add_parser("resume", help="Resume an existing session.")
    resume.add_argument("session_id", help="Session id to resume.")
    _add_common(resume)
    resume.set_defaults(func=_cmd_resume)

    sessions = sub.add_parser("sessions", help="List saved sessions.")
    _add_common(sessions)
    sessions.set_defaults(func=_cmd_sessions)

    validate = sub.add_parser("validate", help="Validate repository configuration offline.")
    _add_common(validate)
    validate.set_defaults(func=_cmd_validate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
