"""Command-line interface for the generic coding agent."""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import replace
from pathlib import Path

from gca.executor.lifecycle import RunLifecycle
from gca.integrations.scm import PublicationPolicy
from gca.jobs.models import JobStatus, RepositorySpec, RunSpec
from gca.jobs.queue import SqliteJobQueue
from gca.jobs.runner import JobRunner
from gca.jobs.store import JobNotFoundError, SqliteJobStore
from gca.model_loading import load_runtime_models
from gca.plugins import LoadedPlugins
from gca.repo_config import RepoConfigError, load_repo_config
from gca.runtime import RuntimeConfig, create_coordinator
from gca.session import SessionStore
from gca.workspace.layout import JobWorkspace, normalize_run_id


def _default_sessions_dir(workspace: Path) -> Path:
    return workspace / ".gca" / "sessions"


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _tool_secret_grants(values: list[str] | None) -> dict[str, frozenset[str]]:
    grants: dict[str, set[str]] = {}
    for value in values or []:
        tool, separator, secret = value.partition("=")
        if separator != "=" or not tool.strip() or not secret.strip():
            raise SystemExit("--allow-tool-secret must use TOOL=ENV_NAME")
        grants.setdefault(tool.strip(), set()).add(secret.strip())
    return {tool: frozenset(names) for tool, names in grants.items()}


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
        *([] if args.trusted_models_only else repo_config.model_paths),
        *(Path(path).resolve() for path in (args.models or [])),
    ]
    return RuntimeConfig(
        workspace=workspace,
        sessions_dir=sessions_dir,
        plugins_dir=plugins_dir,
        skill_dirs=skill_dirs,
        max_steps=(args.max_steps if args.max_steps is not None else repo_config.runtime.max_steps),
        workflow=args.workflow,
        models_paths=models_paths or None,
        repo_config=repo_config,
        trusted_model_paths_only=args.trusted_models_only,
    )


def _load_models(args: argparse.Namespace, config: RuntimeConfig) -> LoadedPlugins:
    try:
        return load_runtime_models(
            config,
            script_path=Path(args.script) if args.script else None,
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


def _event_printer(message: str) -> None:
    print(message, file=sys.stderr)


def _cmd_run(args: argparse.Namespace) -> int:
    source = Path(args.workspace).resolve()
    try:
        source_config = load_repo_config(source)
    except RepoConfigError as exc:
        raise SystemExit(f"Invalid .gca/config.yaml: {exc}") from exc

    runs_root = source / ".gca" / "runs"
    bootstrap_sessions = SessionStore(
        Path(args.sessions_dir) if args.sessions_dir else _default_sessions_dir(source)
    )
    session = bootstrap_sessions.create(args.task)
    print(f"session: {session.id}", file=sys.stderr)

    lifecycle = RunLifecycle.for_local_run(
        source,
        runs_root,
        source_config,
        run_id=session.id,
    )
    result_status = "failed"
    try:
        sessions_dir = (
            Path(args.sessions_dir)
            if args.sessions_dir
            else lifecycle.workspace.parent / "sessions"
        )
        config = replace(
            _build_config(args),
            workspace=lifecycle.workspace,
            sessions_dir=sessions_dir,
            repo_config=load_repo_config(lifecycle.workspace),
            executor=lifecycle.executor,
        )
        store = SessionStore(config.sessions_dir)
        store.save(session)
        loaded = _load_models(args, config)
        coordinator = create_coordinator(
            config,
            loaded.models,
            loaded_plugins=loaded,
            on_event=_event_printer,
        )
        result = coordinator.run(session, store)
        result_status = result.status
        if result.status == "completed":
            synced = lifecycle.sync_back()
            if synced.changed_files:
                print(
                    f"synced {len(synced.changed_files)} file(s) back to {source}",
                    file=sys.stderr,
                )
        else:
            print(
                f"ephemeral workspace preserved at: {lifecycle.workspace}",
                file=sys.stderr,
            )
        print(f"\nstatus: {result.status} (steps: {result.steps})")
        print(result.final_message)
        return 0 if result.status == "completed" else 1
    finally:
        lifecycle.cleanup(wipe_workspace=result_status == "completed")


def _cmd_resume(args: argparse.Namespace) -> int:
    source = Path(args.workspace).resolve()
    config = _build_config(args)
    runs_root = source / ".gca" / "runs"
    ephemeral: JobWorkspace | None = None
    try:
        layout = JobWorkspace.under(runs_root, normalize_run_id(args.session_id))
        if layout.repository.is_dir():
            ephemeral = layout
    except ValueError:
        ephemeral = None

    if ephemeral is not None:
        repo_config = load_repo_config(ephemeral.repository)
        lifecycle = RunLifecycle.for_repository(
            ephemeral.repository,
            repo_config,
            run_id=args.session_id,
        )
        result_status = "failed"
        try:
            config = replace(
                config,
                workspace=ephemeral.repository,
                sessions_dir=ephemeral.sessions,
                repo_config=repo_config,
                executor=lifecycle.executor,
            )
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
            result_status = result.status
            if result.status == "completed":
                sync_lifecycle = RunLifecycle(
                    run_id=lifecycle.run_id,
                    workspace=ephemeral.repository,
                    executor=lifecycle.executor,
                    source_workspace=source,
                    baseline_hashes={},
                )
                synced = sync_lifecycle.sync_back()
                if synced.changed_files:
                    print(
                        f"synced {len(synced.changed_files)} file(s) back to {source}",
                        file=sys.stderr,
                    )
            print(f"\nstatus: {result.status} (steps: {result.steps})")
            print(result.final_message)
            return 0 if result.status == "completed" else 1
        finally:
            lifecycle.cleanup(wipe_workspace=result_status == "completed")
            if result_status == "completed" and ephemeral.root.exists():
                shutil.rmtree(ephemeral.root, ignore_errors=True)

    store = SessionStore(config.sessions_dir)
    loaded = _load_models(args, config)
    session = store.load(args.session_id)
    print(f"resuming session: {session.id}", file=sys.stderr)
    lifecycle = RunLifecycle.for_repository(
        config.workspace,
        config.repo_config or load_repo_config(config.workspace),
        run_id=session.id,
    )
    try:
        config = replace(config, executor=lifecycle.executor)
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
    finally:
        lifecycle.cleanup(wipe_workspace=False)


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
    PublicationPolicy.from_mapping(repo_config.publication)
    print(f"configuration valid (version {repo_config.version})")
    print(f"models: {', '.join(loaded.models.names())}")
    skill_paths = [str(path) for path in config.skill_dirs or []]
    print(f"skills: {', '.join(skill_paths)}")
    print(f"tools: {', '.join(coordinator.tools.names())}")
    print(f"profile: {repo_config.runtime.profile}")
    return 0


def _cmd_job_run(args: argparse.Namespace) -> int:
    """Run one repository job through the same worker path used by the service."""

    job_root = Path(args.job_root).resolve()
    store = SqliteJobStore(job_root / "jobs.sqlite3")
    queue = SqliteJobQueue(store)
    spec = RunSpec(
        task=args.task,
        repository=RepositorySpec(
            url=args.repository,
            ref=args.ref,
            shallow_depth=args.shallow_depth,
        ),
        workflow=args.workflow,
        max_steps=args.max_steps,
    )
    job = store.create(spec, idempotency_key=args.idempotency_key)
    if job.status != JobStatus.QUEUED:
        print(f"job: {job.id}")
        print(f"status: {job.status.value}")
        return 0 if job.status == JobStatus.COMPLETED else 1
    queue.enqueue(job.id)
    return _execute_local_job(args, store, queue, job.id)


def _cmd_job_resume(args: argparse.Namespace) -> int:
    """Resume one paused local job with a larger total step budget."""

    job_root = Path(args.job_root).resolve()
    store = SqliteJobStore(job_root / "jobs.sqlite3")
    queue = SqliteJobQueue(store)
    try:
        job = store.load(args.job_id)
    except JobNotFoundError as exc:
        raise SystemExit(str(exc)) from exc
    if job.status != JobStatus.PAUSED:
        raise SystemExit(f"job cannot be resumed from {job.status.value}")
    prior = job.run_spec.max_steps or 0
    if args.max_steps <= prior:
        raise SystemExit("--max-steps must exceed the prior job step budget")
    job.run_spec = replace(job.run_spec, max_steps=args.max_steps)
    store.save(job)
    queue.enqueue(job.id)
    return _execute_local_job(args, store, queue, job.id)


def _execute_local_job(
    args: argparse.Namespace,
    store: SqliteJobStore,
    queue: SqliteJobQueue,
    job_id: str,
) -> int:
    claimed = queue.claim_job(
        job_id,
        "local-cli",
        lease_seconds=args.lease_seconds,
    )
    if claimed is None:
        raise SystemExit("job could not be claimed")

    script_path = Path(args.script).resolve() if args.script else None

    def model_loader(config: RuntimeConfig) -> LoadedPlugins:
        return load_runtime_models(config, script_path=script_path)

    runner = JobRunner(
        store=store,
        workspace_root=Path(args.job_root).resolve() / "workspaces",
        model_loader=model_loader,
        allowed_repository_hosts=frozenset(args.allowed_host or []),
        allow_local_repositories=args.allow_local_repository,
        plugin_dir=Path(args.plugins).resolve() if args.plugins else None,
        skill_dirs=[Path(value).resolve() for value in args.skills or []] or None,
        model_paths=[Path(value).resolve() for value in args.models or []] or None,
        allowed_tool_secret_grants=_tool_secret_grants(args.allow_tool_secret),
        on_event=_event_printer,
    )
    result = runner.execute(claimed)
    print(f"job: {result.id}")
    print(f"status: {result.status.value}")
    if result.session_id:
        print(f"session: {result.session_id}")
    if result.workspace_path:
        print(f"workspace: {result.workspace_path}")
    if result.last_error:
        print(f"error: {result.last_error}")
    return 0 if result.status == JobStatus.COMPLETED else 1


def _add_job_runtime_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--job-root", default=".gca/jobs")
    parser.add_argument("--lease-seconds", type=_positive_int, default=900)
    parser.add_argument("--allowed-host", action="append", default=None)
    parser.add_argument("--allow-local-repository", action="store_true")
    parser.add_argument("--plugins", default=None)
    parser.add_argument("--models", action="append", default=None)
    parser.add_argument("--skills", action="append", default=None)
    parser.add_argument("--script", default=None)
    parser.add_argument(
        "--allow-tool-secret",
        action="append",
        default=None,
        metavar="TOOL=ENV_NAME",
    )


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
        type=_positive_int,
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
    parser.add_argument(
        "--trusted-models-only",
        action="store_true",
        help="Ignore checkout-local model catalogs; use user/--models catalogs only.",
    )


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

    job = sub.add_parser("job", help="Run durable repository jobs.")
    job_commands = job.add_subparsers(dest="job_command", required=True)
    job_run = job_commands.add_parser("run", help="Clone and run one repository task.")
    job_run.add_argument("task", help="Task description for the agent.")
    job_run.add_argument("--repository", required=True, help="HTTPS, SSH, or allowed local repo.")
    job_run.add_argument("--ref", default="main", help="Branch or tag to clone.")
    job_run.add_argument("--shallow-depth", type=_positive_int, default=1)
    _add_job_runtime_options(job_run)
    job_run.add_argument("--idempotency-key", default=None)
    job_run.add_argument("--max-steps", type=_positive_int, default=None)
    job_run.add_argument(
        "--workflow",
        choices=["auto", "fast", "feature"],
        default=None,
    )
    job_run.set_defaults(func=_cmd_job_run)

    job_resume = job_commands.add_parser("resume", help="Resume a paused repository job.")
    job_resume.add_argument("job_id")
    _add_job_runtime_options(job_resume)
    job_resume.add_argument("--max-steps", type=_positive_int, required=True)
    job_resume.set_defaults(func=_cmd_job_resume)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
