"""Command-line interface for the generic coding agent."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from gca.models import ModelProfile
from gca.plugins import LoadedPlugins, load_plugins
from gca.providers.scripted import ScriptedProvider
from gca.runtime import RuntimeConfig, create_coordinator
from gca.session import SessionStore


def _default_sessions_dir(workspace: Path) -> Path:
    return workspace / ".gca" / "sessions"


def _build_config(args: argparse.Namespace) -> RuntimeConfig:
    workspace = Path(args.workspace).resolve()
    sessions_dir = (
        Path(args.sessions_dir) if args.sessions_dir else _default_sessions_dir(workspace)
    )
    plugins_dir = Path(args.plugins).resolve() if args.plugins else None
    skill_dirs = [Path(d).resolve() for d in args.skills] if args.skills else None
    return RuntimeConfig(
        workspace=workspace,
        sessions_dir=sessions_dir,
        plugins_dir=plugins_dir,
        skill_dirs=skill_dirs,
        max_steps=args.max_steps,
        workflow=args.workflow,
    )


def _load_models(args: argparse.Namespace, config: RuntimeConfig) -> LoadedPlugins:
    loaded = (
        load_plugins(config.plugins_dir)
        if config.plugins_dir is not None
        else LoadedPlugins()
    )
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
        "No models configured. Supply a plugin directory (--plugins) whose modules "
        "define get_models() or get_provider(), or use --script PATH to drive the "
        "built-in scripted provider."
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


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", default=".", help="Workspace root (default: cwd).")
    parser.add_argument("--sessions-dir", default=None, help="Where to store sessions.")
    parser.add_argument("--plugins", default=None, help="Directory of plugin modules.")
    parser.add_argument(
        "--skills", action="append", default=None, help="Skill directory (repeatable)."
    )
    parser.add_argument("--max-steps", type=int, default=25, help="Max agent steps.")
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
