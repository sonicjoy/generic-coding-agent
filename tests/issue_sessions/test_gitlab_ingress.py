"""Tests for registration-bound GitLab event normalization and ingestion."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

from gca.integrations.gitlab_events import (
    GitLabIssueEventNormalizer,
    neutralize_untrusted_markdown,
)
from gca.integrations.webhook_registration import WebhookRegistration
from gca.integrations.webhooks import WebhookContext
from gca.issue_sessions.ingestion import IssueSessionIngestor, StaticMembershipChecker
from gca.issue_sessions.store import IssueSessionStore


def _registration(**overrides: object) -> WebhookRegistration:
    payload = {
        "id": "mmmapper-prod",
        "gitlab_instance": "https://gitlab.com",
        "project_id": 42,
        "project_path": "group/mmmapper",
        "hook_uuid": "hook-uuid-1",
        "signing_secret": "super-secret-signing-token",
        "trigger_label": "gca-run",
        "allow_legacy_token": False,
        "bot_user_id": 999,
        "repository_url": "https://gitlab.com/group/mmmapper.git",
        "actor_allowlist": [7],
    }
    payload.update(overrides)
    return WebhookRegistration.from_mapping(payload)


def _signed_context(body: dict, registration: WebhookRegistration) -> WebhookContext:
    raw = json.dumps(body).encode()
    webhook_id = "delivery-123"
    timestamp = str(int(time.time()))
    digest = hmac.new(
        registration.signing_secret.encode(),
        f"{webhook_id}.{timestamp}.".encode() + raw,
        hashlib.sha256,
    ).digest()
    signature = "v1," + base64.b64encode(digest).decode()
    return WebhookContext(
        provider="gitlab",
        headers={
            "X-Gitlab-Event": body.get("_event", "Issue Hook"),
            "X-Gitlab-Instance": registration.gitlab_instance,
            "X-Gitlab-Webhook-UUID": registration.hook_uuid,
            "X-Gitlab-Event-UUID": "event-uuid-1",
            "webhook-id": webhook_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": signature,
        },
        body=raw,
    )


def test_label_transition_starts_issue_session(tmp_path: Path) -> None:
    registration = _registration()
    store = IssueSessionStore(tmp_path / "db.sqlite3")
    ingestor = IssueSessionIngestor(
        store,
        membership=StaticMembershipChecker({(42, 7): 40}),
    )
    body = {
        "_event": "Issue Hook",
        "object_kind": "issue",
        "user": {"id": 7, "username": "dev"},
        "project": {
            "id": 42,
            "path_with_namespace": "group/mmmapper",
            "git_http_url": "https://gitlab.com/group/mmmapper.git",
            "default_branch": "develop",
        },
        "object_attributes": {
            "action": "update",
            "iid": 11,
            "title": "Fix null metadata",
            "description": "fails in pipeline",
        },
        "labels": [{"title": "gca-run"}],
        "changes": {
            "labels": {
                "previous": [],
                "current": [{"title": "gca-run"}],
            }
        },
    }
    context = _signed_context(body, registration)
    context.headers["X-Gitlab-Event"] = "Issue Hook"
    normalizer = GitLabIssueEventNormalizer(registration)
    normalizer.verify(context)
    event = normalizer.normalize(context)
    result = ingestor.ingest(event, registration=registration)
    assert result.status == "accepted"
    assert result.issue_session_id is not None
    session = store.get_session(result.issue_session_id)
    assert session.issue_iid == 11
    assert result.job_id is not None


def test_exact_agent_run_comment_starts_session(tmp_path: Path) -> None:
    registration = _registration()
    store = IssueSessionStore(tmp_path / "db.sqlite3")
    ingestor = IssueSessionIngestor(
        store,
        membership=StaticMembershipChecker({(42, 7): 40}),
    )
    body = {
        "object_kind": "note",
        "user": {"id": 7, "username": "dev"},
        "project": {
            "id": 42,
            "path_with_namespace": "group/mmmapper",
            "git_http_url": "https://gitlab.com/group/mmmapper.git",
        },
        "object_attributes": {
            "id": 55,
            "note": "/agent run",
            "noteable_type": "Issue",
            "action": "create",
            "system": False,
        },
        "issue": {"iid": 12, "title": "Bug", "description": "details"},
    }
    context = _signed_context(body, registration)
    context.headers["X-Gitlab-Event"] = "Note Hook"
    event = GitLabIssueEventNormalizer(registration).normalize(context)
    assert event.command == "/agent run"
    result = ingestor.ingest(event, registration=registration)
    assert result.status == "accepted"


def test_bot_notes_and_quick_actions_are_neutralized() -> None:
    text = neutralize_untrusted_markdown("/close\nplease @admin help")
    assert "\\/close" in text or text.splitlines()[0].lstrip().startswith("\\/")
    assert "@\u200badmin" in text


def test_duplicate_delivery_is_accepted_as_duplicate(tmp_path: Path) -> None:
    registration = _registration()
    store = IssueSessionStore(tmp_path / "db.sqlite3")
    ingestor = IssueSessionIngestor(
        store,
        membership=StaticMembershipChecker({(42, 7): 40}),
    )
    body = {
        "object_kind": "note",
        "user": {"id": 7, "username": "dev"},
        "project": {
            "id": 42,
            "path_with_namespace": "group/mmmapper",
            "git_http_url": "https://gitlab.com/group/mmmapper.git",
        },
        "object_attributes": {
            "id": 70,
            "note": "/agent run",
            "noteable_type": "Issue",
            "action": "create",
            "system": False,
        },
        "issue": {"iid": 13, "title": "Bug", "description": "details"},
    }
    context = _signed_context(body, registration)
    context.headers["X-Gitlab-Event"] = "Note Hook"
    normalizer = GitLabIssueEventNormalizer(registration)
    event = normalizer.normalize(context)
    first = ingestor.ingest(event, registration=registration)
    second = ingestor.ingest(event, registration=registration)
    assert first.status == "accepted"
    assert second.status == "duplicate"
