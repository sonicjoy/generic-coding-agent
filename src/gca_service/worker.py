"""Asynchronous worker loop for durable repository jobs."""

from __future__ import annotations

import threading
from collections.abc import Callable

from gca.git_credentials import GitCredentials
from gca.integrations.github import GitHubScmAdapter
from gca.integrations.gitlab import GitLabScmAdapter
from gca.integrations.scm import PublicationController, ScmAdapter
from gca.jobs.models import Job, JobStatus, RepositorySpec
from gca.jobs.runner import JobRunner, RuntimeModelLoader
from gca.jobs.store import JobConcurrencyError
from gca.workspace.prepare import repository_host
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
    ) -> None:
        self.state = state
        self.on_event = on_event
        self.model_loader = model_loader

    def run_once(self) -> Job | None:
        """Claim and execute one due job, returning ``None`` when idle."""

        settings = self.state.settings
        job = self.state.queue.claim(
            settings.worker_id,
            lease_seconds=settings.lease_seconds,
        )
        if job is None:
            return None

        def clone_credentials(repository: RepositorySpec) -> GitCredentials | None:
            host = repository_host(repository.url)
            if host == settings.github_host and settings.github_token:
                return GitCredentials("x-access-token", settings.github_token, host)
            if host == settings.gitlab_host and settings.gitlab_token:
                return GitCredentials("oauth2", settings.gitlab_token, host)
            return None

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
                allowed_tool_secrets=settings.allowed_tool_secrets,
            )
            result = runner.execute(job)
            lease.check()
            return result

    def run_forever(self, stop: threading.Event | None = None) -> None:
        """Poll until ``stop`` is set."""

        stopper = stop or threading.Event()
        while not stopper.is_set():
            job = self.run_once()
            if job is None:
                stopper.wait(self.state.settings.poll_seconds)


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
    return PublicationController(adapters) if adapters else None
