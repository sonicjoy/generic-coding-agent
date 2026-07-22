"""Generic job runner: clone, resume agent session, and optionally publish."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from gca.agent import AgentResult, EventHook
from gca.credentials import CredentialBroker
from gca.jobs.lifecycle import can_retry, retry_delay_seconds, transition_job
from gca.jobs.models import Job, JobStatus
from gca.jobs.store import JobStore
from gca.model_loading import load_runtime_models
from gca.plugins import LoadedPlugins
from gca.repo_config import load_repo_config
from gca.runtime import RuntimeConfig, create_coordinator
from gca.session import STATUS_COMPLETED, STATUS_FAILED, STATUS_PAUSED, SessionStore
from gca.workspace.layout import JobWorkspace
from gca.workspace.prepare import WorkspaceError, prepare_repository


class Publisher(Protocol):
    """Service-owned publication hook called only after successful agent completion."""

    def publish(self, job: Job, workspace: Path) -> dict[str, object]: ...


RuntimeModelLoader = Callable[[RuntimeConfig], LoadedPlugins]


class JobRunner:
    """Execute durable jobs in isolated repository workspaces."""

    def __init__(
        self,
        *,
        store: JobStore,
        workspace_root: Path,
        model_loader: RuntimeModelLoader | None = None,
        publisher: Publisher | None = None,
        allowed_repository_hosts: frozenset[str] = frozenset(),
        allow_local_repositories: bool = False,
        hosted_mode: bool = True,
        plugin_dir: Path | None = None,
        skill_dirs: list[Path] | None = None,
        model_paths: list[Path] | None = None,
        on_event: EventHook | None = None,
    ) -> None:
        self.store = store
        self.workspace_root = Path(workspace_root).resolve()
        self.model_loader = model_loader or load_runtime_models
        self.publisher = publisher
        self.allowed_repository_hosts = allowed_repository_hosts
        self.allow_local_repositories = allow_local_repositories
        self.hosted_mode = hosted_mode
        self.plugin_dir = plugin_dir
        self.skill_dirs = skill_dirs
        self.model_paths = model_paths
        self.on_event = on_event

    def execute(self, job: Job) -> Job:
        """Run a claimed job to a durable terminal, paused, or retry state."""

        if job.status != JobStatus.RUNNING:
            raise ValueError("job must be claimed before execution")
        layout = JobWorkspace.under(self.workspace_root, job.id)
        layout.ensure_metadata()
        try:
            repository = prepare_repository(
                job.run_spec.repository,
                layout.repository,
                allowed_hosts=self.allowed_repository_hosts,
                allow_local=self.allow_local_repositories,
                env=CredentialBroker.from_environment().subprocess_env("hosted"),
            )
            job.workspace_path = str(repository)
            self.store.save(job)
            result = self._run_agent(job, layout)
            self._apply_result(job, result, repository)
        except Exception as exc:
            self._handle_failure(job, exc)
        return job

    def _run_agent(self, job: Job, layout: JobWorkspace) -> AgentResult:
        repo_config = load_repo_config(layout.repository)
        if self.hosted_mode and repo_config.runtime.profile != "hosted":
            repo_config = replace(
                repo_config,
                runtime=replace(repo_config.runtime, profile="hosted"),
            )
        max_steps = job.run_spec.max_steps or repo_config.runtime.max_steps
        runtime = RuntimeConfig(
            workspace=layout.repository,
            sessions_dir=layout.sessions,
            plugins_dir=self.plugin_dir,
            skill_dirs=self.skill_dirs,
            max_steps=max_steps,
            workflow=job.run_spec.workflow,
            models_paths=self.model_paths or list(repo_config.model_paths) or None,
            repo_config=repo_config,
        )
        loaded = self.model_loader(runtime)
        sessions = SessionStore(layout.sessions)
        if job.session_id is None:
            session = sessions.create(job.run_spec.task)
            job.session_id = session.id
            self.store.save(job)
        else:
            session = sessions.load(job.session_id)
        coordinator = create_coordinator(
            runtime,
            loaded.models,
            loaded_plugins=loaded,
            on_event=self.on_event,
        )
        return coordinator.run(session, sessions)

    def _apply_result(self, job: Job, result: AgentResult, repository: Path) -> None:
        if result.status == STATUS_PAUSED:
            transition_job(job, JobStatus.PAUSED, error=result.final_message)
            self.store.save(job)
            return
        if result.status == STATUS_FAILED:
            transition_job(job, JobStatus.FAILED, error=result.final_message)
            self.store.save(job)
            return
        if result.status != STATUS_COMPLETED:
            raise RuntimeError(f"unsupported agent result status: {result.status}")
        if job.run_spec.publication is None:
            transition_job(job, JobStatus.COMPLETED)
            self.store.save(job)
            return
        if self.publisher is None:
            transition_job(
                job,
                JobStatus.FAILED,
                error="publication requested but no SCM publisher is configured",
            )
            self.store.save(job)
            return
        transition_job(job, JobStatus.PUBLISHING)
        self.store.save(job)
        job.publication = dict(self.publisher.publish(job, repository))
        transition_job(job, JobStatus.COMPLETED)
        self.store.save(job)

    def _handle_failure(self, job: Job, exc: Exception) -> None:
        message = f"{type(exc).__name__}: {exc}"
        transient = isinstance(exc, (WorkspaceError, TimeoutError, ConnectionError))
        if transient and can_retry(job):
            transition_job(job, JobStatus.QUEUED, error=message)
            job.not_before = time.time() + retry_delay_seconds(job.attempt)
        else:
            transition_job(job, JobStatus.FAILED, error=message)
        self.store.save(job)
