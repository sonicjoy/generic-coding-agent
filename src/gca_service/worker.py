"""Asynchronous worker loop for durable repository jobs."""

from __future__ import annotations

import threading
from collections.abc import Callable

from gca.git_credentials import GitCredentials
from gca.integrations.github import GitHubScmAdapter
from gca.integrations.gitlab import GitLabScmAdapter
from gca.integrations.scm import PublicationController, ScmAdapter
from gca.jobs.models import Job, RepositorySpec
from gca.jobs.runner import JobRunner, RuntimeModelLoader
from gca.workspace.prepare import repository_host
from gca_service.state import ServiceState

EventSink = Callable[[str], None]


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

        def heartbeat(active: Job) -> None:
            renewed = self.state.store.renew_lease(
                active.id,
                settings.worker_id,
                lease_seconds=settings.lease_seconds,
            )
            active.version = renewed.version
            active.lease_expires_at = renewed.lease_expires_at

        def clone_credentials(repository: RepositorySpec) -> GitCredentials | None:
            host = repository_host(repository.url)
            if host == settings.github_host and settings.github_token:
                return GitCredentials("x-access-token", settings.github_token)
            if host == settings.gitlab_host and settings.gitlab_token:
                return GitCredentials("oauth2", settings.gitlab_token)
            return None

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
            lease_heartbeat=heartbeat,
            repository_credentials=clone_credentials,
        )
        return runner.execute(job)

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
        )
    if settings.gitlab_token:
        adapters["gitlab"] = GitLabScmAdapter(
            settings.gitlab_token,
            api_url=settings.gitlab_api_url,
        )
    return PublicationController(adapters) if adapters else None
