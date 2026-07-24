"""Generic job runner: clone, resume agent session, and optionally publish."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from gca.agent import AgentResult, EventHook
from gca.credentials import CredentialBroker
from gca.executor.lifecycle import RunLifecycle, should_wipe_workspace
from gca.executor.protocol import CommandExecutor
from gca.git_credentials import GitCredentials
from gca.integrations.http import IntegrationHttpError
from gca.integrations.scm import PublicationError
from gca.jobs.artifacts import persist_job_artifacts
from gca.jobs.lifecycle import can_retry, retry_delay_seconds, transition_job
from gca.jobs.models import Job, JobStatus, RepositorySpec
from gca.jobs.store import JobStore
from gca.model_loading import load_runtime_models
from gca.plugins import LoadedPlugins
from gca.providers.base import ProviderError
from gca.repo_config import RepoConfig, load_repo_config
from gca.runtime import RuntimeConfig, create_coordinator
from gca.session import STATUS_COMPLETED, STATUS_FAILED, STATUS_PAUSED, Session, SessionStore
from gca.usage import totals_from_dict
from gca.workspace.layout import JobWorkspace
from gca.workspace.prepare import WorkspaceError, prepare_repository


class Publisher(Protocol):
    """Service-owned publication hook called only after successful agent completion."""

    def publish(
        self,
        job: Job,
        workspace: Path,
        repo_config: RepoConfig,
        *,
        executor: CommandExecutor | None = None,
    ) -> dict[str, object]: ...


RuntimeModelLoader = Callable[[RuntimeConfig], LoadedPlugins]
LeaseHeartbeat = Callable[[Job], None]
RepositoryCredentialResolver = Callable[[RepositorySpec], GitCredentials | None]
ExecutorFactory = Callable[[Path, RepoConfig, str], CommandExecutor]


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
        allowed_tool_secret_grants: Mapping[str, frozenset[str]] | None = None,
        executor_factory: ExecutorFactory | None = None,
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
        self.allowed_tool_secret_grants = dict(allowed_tool_secret_grants or {})
        self.executor_factory = executor_factory
        self.credentials = CredentialBroker.from_environment()

    def execute(self, job: Job) -> Job:
        """Run a claimed job to a durable terminal, paused, or retry state."""

        if job.status != JobStatus.RUNNING:
            raise ValueError("job must be claimed before execution")
        layout = JobWorkspace.under(self.workspace_root, job.id)
        layout.ensure_metadata()
        lifecycle: RunLifecycle | None = None
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
            repo_config = self._load_repo_config(layout.repository)
            lifecycle = self._build_lifecycle(job, layout.repository, repo_config)
            result, implementation_summary = self._run_agent(
                job, layout, repo_config, lifecycle.executor
            )
            self._heartbeat(job)
            self._apply_result(
                job,
                result,
                repository,
                repo_config,
                lifecycle.executor,
                implementation_summary=implementation_summary,
            )
        except Exception as exc:
            self._handle_failure(job, exc)
        finally:
            if layout.repository.exists():
                try:
                    persist_job_artifacts(layout, job, layout.repository)
                except OSError:
                    pass
            if lifecycle is not None:
                lifecycle.cleanup(wipe_workspace=should_wipe_workspace(job.status.value))
        return job

    def _load_repo_config(self, repository: Path) -> RepoConfig:
        repo_config = load_repo_config(repository)
        if self.hosted_mode and repo_config.runtime.profile != "hosted":
            return replace(
                repo_config,
                runtime=replace(repo_config.runtime, profile="hosted"),
            )
        return repo_config

    def _build_lifecycle(
        self,
        job: Job,
        repository: Path,
        repo_config: RepoConfig,
    ) -> RunLifecycle:
        unauthorized_grants = {
            tool: sorted(secrets - self.allowed_tool_secret_grants.get(tool, frozenset()))
            for tool, secrets in repo_config.tools.secret_access.items()
        }
        unauthorized_grants = {
            tool: secrets for tool, secrets in unauthorized_grants.items() if secrets
        }
        if self.hosted_mode and unauthorized_grants:
            details = "; ".join(
                f"{tool}={','.join(secrets)}"
                for tool, secrets in sorted(unauthorized_grants.items())
            )
            raise ValueError(
                "hosted repository requested unapproved tool secret grants: " + details
            )
        executor = (
            self.executor_factory(repository, repo_config, job.id)
            if self.executor_factory is not None
            else None
        )
        return RunLifecycle.for_repository(
            repository,
            repo_config,
            run_id=job.id,
            executor=executor,
        )

    def _run_agent(
        self,
        job: Job,
        layout: JobWorkspace,
        repo_config: RepoConfig,
        executor: CommandExecutor,
    ) -> tuple[AgentResult, str | None]:
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
            executor=executor,
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
        result = coordinator.run(session, sessions)
        # Session usage is cumulative across resumes; copy absolute totals onto the
        # durable job record so cost survives workspace wipe.
        job.llm_usage = totals_from_dict(session.llm_usage).to_dict()
        self.store.save(job)
        return result, _implementation_summary(session)

    def _apply_result(
        self,
        job: Job,
        result: AgentResult,
        repository: Path,
        repo_config: RepoConfig,
        executor: CommandExecutor,
        *,
        implementation_summary: str | None = None,
    ) -> None:
        job.result_summary = self.credentials.redact(result.final_message)
        outcome = result.outcome_kind
        if result.status == STATUS_PAUSED and outcome == "needs_human":
            # Human-wait is a successful turn completion for the job/outbox layer.
            transition_job(job, JobStatus.COMPLETED)
            self.store.save(job)
            return
        if result.status == STATUS_PAUSED:
            pause_error = _budget_pause_message(job, result.final_message)
            # Finished implementations are draft-published so operators waiting on
            # publication.change_request_url still get a visible recoverable artifact.
            if (
                implementation_summary
                and job.run_spec.publication is not None
                and self.publisher is not None
            ):
                try:
                    transition_job(job, JobStatus.PUBLISHING)
                    self.store.save(job)
                    self._heartbeat(job)
                    original = job.run_spec.publication
                    job.run_spec = replace(
                        job.run_spec,
                        publication=replace(original, draft=True),
                    )
                    try:
                        job.publication = dict(
                            self.publisher.publish(job, repository, repo_config, executor=executor)
                        )
                    finally:
                        # Restore the operator-requested draft flag for resume/complete.
                        job.run_spec = replace(job.run_spec, publication=original)
                    url = str(job.publication.get("change_request_url") or "").strip()
                    if url:
                        pause_error = f"{pause_error} Draft change request opened: {url}"
                except PublicationError as exc:
                    detail = self.credentials.redact(str(exc))
                    pause_error = f"{pause_error} Draft publication on pause failed: {detail}"
            transition_job(job, JobStatus.PAUSED, error=pause_error)
            self.store.save(job)
            self._emit_job_status(job)
            return
        if result.status == STATUS_FAILED:
            transition_job(job, JobStatus.FAILED, error=result.final_message)
            self.store.save(job)
            self._emit_job_status(job)
            return
        if result.status != STATUS_COMPLETED:
            raise RuntimeError(f"unsupported agent result status: {result.status}")
        if job.run_spec.publication is None:
            transition_job(job, JobStatus.COMPLETED)
            self.store.save(job)
            return
        if self.publisher is None:
            provider = job.run_spec.publication.provider
            env_var = {
                "github": "GCA_GITHUB_TOKEN",
                "gitlab": "GCA_GITLAB_TOKEN",
            }.get(provider)
            error = f"publication to '{provider}' requested but no SCM publisher is configured" + (
                f" (is {env_var} set?)" if env_var else ""
            )
            transition_job(
                job,
                JobStatus.FAILED,
                error=error,
            )
            self.store.save(job)
            self._emit_job_status(job)
            return
        transition_job(job, JobStatus.PUBLISHING)
        self.store.save(job)
        provider = job.run_spec.publication.provider
        self._emit(
            f"[job] event=publish_start job_id={job.id} provider={provider} "
            f"session_id={job.session_id or ''}"
        )
        self._heartbeat(job)
        try:
            job.publication = dict(
                self.publisher.publish(job, repository, repo_config, executor=executor)
            )
        except PublicationError as exc:
            detail = self.credentials.redact(str(exc))
            self._emit(
                f"[job] event=publish_failure job_id={job.id} provider={provider} error={detail}"
            )
            raise
        url = str(job.publication.get("change_request_url") or "").strip()
        self._emit(
            f"[job] event=publish_success job_id={job.id} provider={provider} "
            f"change_request_url={url or 'none'}"
        )
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
        # Publication and other post-agent failures must appear in the worker log,
        # not only on the job record returned by GET /runs/{id}.
        self._emit_job_status(job)

    def _heartbeat(self, job: Job) -> None:
        if self.lease_heartbeat is not None:
            self.lease_heartbeat(job)

    def _emit(self, message: str) -> None:
        if self.on_event is not None:
            self.on_event(message)

    def _emit_job_status(self, job: Job) -> None:
        """Emit durable job status (and last_error when set) to the event sink."""

        line = f"[job] {job.id} {job.status.value}"
        if job.last_error:
            line = f"{line}: {job.last_error}"
        self._emit(line)

    def _on_agent_event(self, job: Job, message: str) -> None:
        self._heartbeat(job)
        if message.startswith("[routing]"):
            self._emit(f"[job] job_id={job.id} {message}")
        else:
            self._emit(message)


def _implementation_summary(session: Session) -> str | None:
    """Return a non-empty implementation artifact from the session workflow, if any."""

    workflow = session.workflow
    if workflow is None:
        return None
    summary = str(workflow.artifacts.get("implementation") or "").strip()
    return summary or None


def _budget_pause_message(job: Job, final_message: str) -> str:
    """Build a recoverable pause signal for generic /runs and webhook jobs."""

    summary = (final_message or "Step budget exhausted.").strip()
    return (
        f"{summary} "
        f"Resume with POST /runs/{job.id}/resume (max_steps must exceed the prior "
        "budget) or an authorized /agent fix comment."
    )
