\"""Shared service dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field

from gca.integrations.github import GitHubWebhookNormalizer
from gca.integrations.gitlab import GitLabWebhookNormalizer
from gca.integrations.gitlab_events import GitLabIssueEventNormalizer
from gca.integrations.webhook_registration import WebhookRegistration
from gca.jobs.models import PublicationTarget
from gca.integrations.webhooks import WebhookNormalizer
from gca.issue_sessions.ingestion import IssueSessionIngestor, StaticMembershipChecker
from gca.issue_sessions.store import IssueSessionStore
from gca.jobs.queue import SqliteJobQueue
from gca.jobs.store import SqliteJobStore
from gca_service.config import ServiceSettings


@dataclass
class ServiceState:
    """Dependencies injected into HTTP routes and workers."""

    settings: ServiceSettings
    store: SqliteJobStore
    queue: SqliteJobQueue
    normalizers: dict[str, WebhookNormalizer]
    issue_store: IssueSessionStore
    issue_ingestor: IssueSessionIngestor
    gitlab_registrations: dict[str, WebhookRegistration] = field(default_factory=dict)

    @classmethod
    def build(cls, settings: ServiceSettings) -> ServiceState:
        """Construct default durable state and provider normalizers."""

        settings.validate()
        store = SqliteJobStore(settings.database_path)
        issue_store = IssueSessionStore(settings.database_path)
        membership = StaticMembershipChecker(settings.membership_access_levels)
        registrations = dict(settings.gitlab_webhook_registrations)
        if (
            not registrations
            and settings.gitlab_webhook_secret
            and settings.allowed_gitlab_projects
        ):
            # Legacy single-registration compatibility for one allowlisted project.
            project_path = sorted(settings.allowed_gitlab_projects)[0]
            registrations = {
                "legacy": WebhookRegistration(
                    id="legacy",
                    gitlab_instance=f"https://{settings.gitlab_host}",
                    project_id=1,
                    project_path=project_path,
                    hook_uuid="legacy",
                    signing_secret=settings.gitlab_webhook_secret,
                    trigger_label=settings.gitlab_trigger_label,
                    allow_legacy_token=True,
                    bot_user_id=settings.bot_user_id,
                    repository_url=(f"https://{settings.gitlab_host}/{project_path}.git"),
                )
            }
        return cls(
            settings=settings,
            store=store,
            queue=SqliteJobQueue(store),
            normalizers={
                "github": GitHubWebhookNormalizer(trigger_label=settings.github_trigger_label),
                "gitlab": GitLabWebhookNormalizer(trigger_label=settings.gitlab_trigger_label),
            },
            issue_store=issue_store,
            issue_ingestor=IssueSessionIngestor(issue_store, settings=settings, membership=membership),
            gitlab_registrations=registrations,
        )

    def gitlab_normalizer(self, registration_id: str) -> GitLabIssueEventNormalizer:
        registration = self.gitlab_registrations.get(registration_id)
        if registration is None:
            raise KeyError(registration_id)
        return GitLabIssueEventNormalizer(registration)

    def can_publish(self, publication: PublicationTarget | None) -> tuple[bool, str | None]:
        """Delegates to settings.can_publish."""
        return self.settings.can_publish(publication)
