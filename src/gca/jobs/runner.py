"""Generic job runner: clone, resume agent session, and optionally publish."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from gca.agent import AgentResult, EventHook
from gca.credentials import CredentialBroker
from gca.git_credentials import GitCredentials
from gca.integrations.http import IntegrationHttpError
from gca.integrations.scm import PublicationError
from gca.jobs.lifecycle import can_retry, retry_delay_seconds, transition_job
from gca.jobs.models import Job, JobStatus, RepositorySpec
from gca.jobs.store import JobStore
from gca.model_loading import load_runtime_models
from gca.plugins import LoadedPlugins
from gca.providers.base import ProviderError
from gca.repo_config import load_repo_config
from gca.runtime import RuntimeConfig, create_coordinator
from gca.session import STATUS_COMPLETED, STATUS_FAILED, STATUS_PAUSED, SessionStore
from gca.workspace.layout import JobWorkspace
from gca.workspace.prepare import WorkspaceError, prepare_repository


class Publisher(Protocol):
    """Service-owned publication hook called only after successful agent completion."""

    def publish(self, job: Job, workspace: Path) -> dict[str, object]: ...


RuntimeModelLoader = Callable[[RuntimeConfig], LoadedPlugins]
LeaseHeartbeat = Callable[[Job], None]
RepositoryCredentialResolver = Callable[[RepositorySpec], GitCredentials | None]


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
        lease_heartbeat: LeaseHeartbeat | None = None,
        repository_credentials: RepositoryCredentialResolver | None = None,
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
        self.lease_heartbeat = lease_heartbeat
        self.repository_credentials = repository_credentials
        self.credentials = CredentialBroker.from_environment()

    def execute(self, job: Job) -> Job:
        """Run a claimed job to a durable terminal, paused, or retry state."""

        if job.status != JobStatus.RUNNING:
            raise ValueError("job must be claimed before execution")
        layout = JobWorkspace.under(self.workspace_root, job.id)
        layout.ensure_metadata()
        try:
            self._heartbeat(job)
            repository_destination = layout.repository.resolve()
            if layout.root not in repository_destination.parents:
                raise WorkspaceError("repository workspace escapes its job directory")
            repository = prepare_repository(
                job.run_spec.repository,
                repository_destination,
                allowed_hosts=self.allowed_repository_hosts,
                allow_local=self.allow_local_repositories,
                env=self.credentials.subprocess_env("local"),
                credentials=(
                    self.repository_credentials(job.run_spec.repository)
                    if self.repository_credentials is not None
                    else None
                ),
            )
            job.workspace_path = str(repository)
            self.store.save(job)
            self._heartbeat(job)
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
            models_paths=(
                self.model_paths
                if self.hosted_mode
                else self.model_paths or list(repo_config.model_paths) or None
            ),
            repo_config=repo_config,
            trusted_model_paths_only=self.hosted_mode,
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
            on_event=lambda message: self._on_agent_event(job, message),
        )
        return coordinator.run(session, sessions)

    def _apply_result(self, job: Job, result: AgentResult, repository: Path) -> None:
        job.result_summary = self.credentials.redact(result.final_message)
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
        message = self.credentials.redact(f"{type(exc).__name__}: {exc}")
        transient = (
            isinstance(exc, (WorkspaceError, TimeoutError, ConnectionError))
            or (isinstance(exc, ProviderError) and exc.retryable)
            or (isinstance(exc, IntegrationHttpError) and exc.retryable)
            or (isinstance(exc, PublicationError) and exc.retryable)
        )
        if transient and can_retry(job):
            transition_job(job, JobStatus.QUEUED, error=message)
            job.not_before = time.time() + retry_delay_seconds(job.attempt)
        else:
            transition_job(job, JobStatus.FAILED, error=message)
        self.store.save(job)

    def _heartbeat(self, job: Job) -> None:
        if self.lease_heartbeat is not None:
            self.lease_heartbeat(job)

    def _on_agent_event(self, job: Job, message: str) -> None:
        self._heartbeat(job)
        if self.on_event is not None:
            self.on_event(message)
