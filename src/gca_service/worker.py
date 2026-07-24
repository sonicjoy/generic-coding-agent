"""Asynchronous worker loop for durable repository jobs."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path

from gca.agent import AgentResult
from gca.git_credentials import GitCredentials
from gca.integrations.github import GitHubScmAdapter
from gca.integrations.gitlab import GitLabScmAdapter
from gca.integrations.repository import repository_identity
from gca.integrations.scm import PublicationController, ScmAdapter
from gca.issue_sessions.models import Turn
from gca.issue_sessions.outbox import HttpGitLabApiClient, OutboxProcessor, RecordingGitLabApiClient
from gca.issue_sessions.outcomes import TurnOutcomeApplicator
from gca.issue_sessions.retention import RetentionJanitor
from gca.jobs.models import Job, JobStatus, RepositorySpec, utc_now
from gca.jobs.runner import JobRunner, RuntimeModelLoader
from gca.jobs.store import JobConcurrencyError
from gca.session import SessionStore
from gca.workspace.prepare import repository_host
from gca_service.events import structured_event
from gca_service.issue_progress import announce_github_issue_start
from gca_service.state import ServiceState

EventSink = Callable[[str], None]


class _LeaseKeeper:
    def __init__(self, state: ServiceState, job: Job) -> None:
        self.state = state
        self.job = job
        self.stop = threading.Event()
        self.error: Exception | None = None
        self.thread = threading.Thread(
            target=self._run,
            name=f"gca-lease-{job.id[:8]}",
            daemon=True,
        )

    def __enter__(self) -> _LeaseKeeper:
        self.touch()
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop.set()
        self.thread.join(timeout=5)

    def touch(self) -> None:
        self.check()
        self.state.store.touch_lease(
            self.job.id,
            self.state.settings.worker_id,
            lease_seconds=self.state.settings.lease_seconds,
        )

    def check(self) -> None:
        if self.error is not None:
            raise JobConcurrencyError(f"job lease heartbeat failed: {self.error}")

    def _run(self) -> None:
        interval = min(max(self.state.settings.lease_seconds / 3, 0.1), 30)
        while not self.stop.wait(interval):
            try:
                self.touch()
            except Exception as exc:
                try:
                    current = self.state.store.load(self.job.id)
                    if current.status in {
                        JobStatus.COMPLETED,
                        JobStatus.FAILED,
                        JobStatus.PAUSED,
                        JobStatus.CANCELLED,
                    }:
                        return
                except Exception:
                    pass
                self.error = exc
                self.stop.set()
                return


class ServiceWorker:
    """Poll, lease, execute, and durably finish queued jobs."""

    def __init__(
        self,
        state: ServiceState,
        *,
        on_event: EventSink | None = None,
        model_loader: RuntimeModelLoader | None = None,
        outbox_processor: OutboxProcessor | None = None,
    ) -> None:
        self.state = state
        self.on_event = on_event
        self.model_loader = model_loader
        self.outbox_processor = outbox_processor or _default_outbox(state)
        self._idle_ticks = 0
        self._active_job_id: str | None = None
        self._active_lock = threading.Lock()

    def run_once(self) -> Job | None:
        """Claim and execute one due job, returning ``None`` when idle."""

        settings = self.state.settings
        self.outbox_processor.process_pending()
        job = self.state.queue.claim(
            settings.worker_id,
            lease_seconds=settings.lease_seconds,
        )
        if job is None:
            self._idle_ticks += 1
            if self._idle_ticks % 30 == 0:
                RetentionJanitor(
                    self.state.issue_store,
                    workspace_root=settings.workspace_root,
                    workspace_retention_seconds=settings.workspace_retention_seconds,
                    log_retention_seconds=settings.log_retention_seconds,
                ).run()
            return None
        self._idle_ticks = 0
        self._emit(
            structured_event(
                "worker",
                "claim",
                job_id=job.id,
                attempt=job.attempt,
                max_attempts=job.max_attempts,
                max_steps=job.run_spec.max_steps,
                workflow=job.run_spec.workflow,
                lease_owner=settings.worker_id,
                lease_seconds=settings.lease_seconds,
                issue_id=job.run_spec.labels.get("issue_id"),
                source=job.run_spec.labels.get("source"),
            )
        )
        with self._active_lock:
            self._active_job_id = job.id

        def clone_credentials(repository: RepositorySpec) -> GitCredentials | None:
            host = repository_host(repository.url)
            if host == settings.github_host and settings.github_token:
                return GitCredentials("x-access-token", settings.github_token, host)
            if host == settings.gitlab_host and settings.gitlab_token:
                return GitCredentials("oauth2", settings.gitlab_token, host)
            return None

        try:
            tool_secret_grants = settings.tool_secret_grants.get(
                repository_identity(job.run_spec.repository.url),
                {},
            )
        except ValueError:
            tool_secret_grants = {}

        announce_github_issue_start(job, settings, on_event=self.on_event)

        try:
            with _LeaseKeeper(self.state, job) as lease:
                runner = JobRunner(
                    store=self.state.store,
                    workspace_root=settings.workspace_root,
                    model_loader=self.model_loader,
                    publisher=_publisher(self.state),
                    allowed_repository_hosts=settings.allowed_repository_hosts,
                    allow_local_repositories=settings.allow_local_repositories,
                    hosted_mode=True,
                    plugin_dir=settings.plugin_dir,
                    model_paths=list(settings.model_paths) or None,
                    on_event=self.on_event,
                    lease_heartbeat=lambda active: lease.touch(),
                    repository_credentials=clone_credentials,
                    allowed_tool_secret_grants=tool_secret_grants,
                )
                result = runner.execute(job)
                self._finalize_issue_turn(result)
                self.outbox_processor.process_pending()
                lease.check()
                self._emit(
                    structured_event(
                        "worker",
                        "job_done",
                        job_id=result.id,
                        status=result.status.value,
                        session_id=result.session_id,
                        last_error=result.last_error,
                    )
                )
                return result
        finally:
            with self._active_lock:
                if self._active_job_id == job.id:
                    self._active_job_id = None

    def release_active_lease(self) -> Job | None:
        """Best-effort requeue of the in-flight job (SIGTERM / operator stop)."""

        with self._active_lock:
            job_id = self._active_job_id
        if not job_id:
            return None
        released = self.state.store.release_lease(job_id, self.state.settings.worker_id)
        if released is not None and self.on_event is not None:
            self.on_event(
                f"[worker] event=lease_released job_id={released.id} "
                f"status={released.status.value}"
            )
        return released

    def _emit(self, message: str) -> None:
        if self.on_event is not None:
            self.on_event(message)

    def run_forever(self, stop: threading.Event | None = None) -> None:
        """Poll until ``stop`` is set."""

        stopper = stop or threading.Event()
        while not stopper.is_set():
            job = self.run_once()
            if job is None:
                stopper.wait(self.state.settings.poll_seconds)
                continue
            # Mirror --once terminal lines so redirected worker logs include
            # durable outcomes (including last_error) even when JobRunner events
            # were sparse.
            if self.on_event is not None:
                line = f"{job.id} {job.status.value}"
                if job.last_error:
                    line = f"{line}: {job.last_error}"
                self.on_event(line)

    def _finalize_issue_turn(self, job: Job) -> None:
        turn_id = job.run_spec.labels.get("turn_id")
        if not turn_id:
            return
        workspace = job.workspace_path
        if workspace:
            with self.state.issue_store.unit_of_work() as uow:
                row = uow.connection.execute(
                    "SELECT data FROM issue_turns WHERE id = ?",
                    (turn_id,),
                ).fetchone()
                if row is not None:
                    turn = Turn.from_dict(json.loads(str(row["data"])))
                    turn.workspace_path = workspace
                    turn.agent_session_id = job.session_id
                    uow.save_turn(turn)
                    marker = Path(workspace).parent / "retention.json"
                    marker.write_text(
                        json.dumps(
                            {
                                "status": job.status.value,
                                "updated_at": utc_now(),
                                "turn_id": turn_id,
                            }
                        ),
                        encoding="utf-8",
                    )
        if job.session_id and job.workspace_path:
            sessions = SessionStore(Path(job.workspace_path).parent / "sessions")
            try:
                session = sessions.load(job.session_id)
            except FileNotFoundError:
                session = None
        else:
            session = None
        result = AgentResult(
            status=(
                "paused"
                if job.status == JobStatus.PAUSED
                else "failed"
                if job.status == JobStatus.FAILED
                else "completed"
            ),
            steps=session.step_count if session is not None else 0,
            final_message=job.result_summary or job.last_error,
            outcome_kind=(
                str(session.provider_state.get("outcome_kind")) if session is not None else None
            ),
        )
        # needs_human is stored as completed job with paused agent session outcome.
        if session is not None and session.provider_state.get("outcome_kind") == "needs_human":
            result = AgentResult(
                status="paused",
                steps=session.step_count,
                final_message=session.final_message,
                outcome_kind="needs_human",
            )
        TurnOutcomeApplicator(self.state.issue_store).apply(turn_id=turn_id, result=result)


def _publisher(state: ServiceState) -> PublicationController | None:
    settings = state.settings
    adapters: dict[str, ScmAdapter] = {}
    if settings.github_token:
        adapters["github"] = GitHubScmAdapter(
            settings.github_token,
            api_url=settings.github_api_url,
            git_host=settings.github_host,
        )
    if settings.gitlab_token:
        adapters["gitlab"] = GitLabScmAdapter(
            settings.gitlab_token,
            api_url=settings.gitlab_api_url,
            git_host=settings.gitlab_host,
        )
    return (
        PublicationController(
            adapters,
            tool_secret_grants=state.settings.tool_secret_grants,
            open_change_requests=settings.publish_mode != "branch",
        )
        if adapters
        else None
    )


def _default_outbox(state: ServiceState) -> OutboxProcessor:
    settings = state.settings
    if settings.gitlab_token:
        api: RecordingGitLabApiClient | HttpGitLabApiClient = HttpGitLabApiClient(
            settings.gitlab_token,
            api_url=settings.gitlab_api_url,
        )
    else:
        api = RecordingGitLabApiClient()
    return OutboxProcessor(
        state.issue_store,
        api,
        git_token=settings.gitlab_token,
        allow_auto_merge_projects=settings.allow_auto_merge_projects,
    )
