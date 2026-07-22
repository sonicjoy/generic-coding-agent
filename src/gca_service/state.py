"""Shared service dependencies."""

from __future__ import annotations

from dataclasses import dataclass

from gca.integrations.github import GitHubWebhookNormalizer
from gca.integrations.gitlab import GitLabWebhookNormalizer
from gca.integrations.webhooks import WebhookNormalizer
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

    @classmethod
    def build(cls, settings: ServiceSettings) -> ServiceState:
        """Construct default durable state and provider normalizers."""

        settings.validate()
        store = SqliteJobStore(settings.database_path)
        return cls(
            settings=settings,
            store=store,
            queue=SqliteJobQueue(store),
            normalizers={
                "github": GitHubWebhookNormalizer(trigger_label=settings.github_trigger_label),
                "gitlab": GitLabWebhookNormalizer(trigger_label=settings.gitlab_trigger_label),
            },
        )
