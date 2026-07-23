"""Optional GitHub issue assignment and progress comments for claimed jobs."""

from __future__ import annotations

from collections.abc import Callable

from gca.integrations.github import GitHubScmAdapter
from gca.integrations.http import IntegrationHttpError
from gca.jobs.models import Job
from gca_service.config import ServiceSettings

EventSink = Callable[[str], None]


def announce_github_issue_start(
    job: Job,
    settings: ServiceSettings,
    *,
    on_event: EventSink | None = None,
    adapter: GitHubScmAdapter | None = None,
) -> None:
    """Assign/comment on the originating GitHub issue when enabled.

    Failures are logged and never abort the agent run (missing ``issues``
    permission should not block coding work).
    """

    if not settings.github_issue_assign and not settings.github_issue_progress_comments:
        return
    labels = job.run_spec.labels
    if labels.get("provider") != "github" or not labels.get("issue_id"):
        return
    if not settings.github_token:
        _emit(
            on_event,
            f"[worker] event=issue_progress_skip job_id={job.id} reason=missing_github_token",
        )
        return
    issue_id = str(labels["issue_id"])
    repository_url = job.run_spec.repository.url
    client = adapter or GitHubScmAdapter(
        settings.github_token,
        api_url=settings.github_api_url,
        git_host=settings.github_host,
    )
    try:
        if settings.github_issue_assign:
            login = (settings.github_bot_user or "").strip() or client.authenticated_login()
            client.assign_issue(repository_url, issue_id, [login])
            _emit(
                on_event,
                f"[worker] event=issue_assigned job_id={job.id} issue_id={issue_id} "
                f"assignee={login}",
            )
        if settings.github_issue_progress_comments:
            body = (
                f"GCA started job `{job.id}`"
                + (f" (session `{job.session_id}`)" if job.session_id else "")
                + "."
            )
            url = client.create_issue_comment(repository_url, issue_id, body)
            _emit(
                on_event,
                f"[worker] event=issue_comment job_id={job.id} issue_id={issue_id} "
                f"comment_url={url or 'none'}",
            )
    except (IntegrationHttpError, ValueError) as exc:
        _emit(
            on_event,
            f"[worker] event=issue_progress_error job_id={job.id} "
            f"error={str(exc).replace(chr(10), ' ')}",
        )


def _emit(on_event: EventSink | None, message: str) -> None:
    if on_event is not None:
        on_event(message)
