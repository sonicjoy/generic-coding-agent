from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from gca.integrations.github import GitHubWebhookNormalizer
from gca.integrations.gitlab import GitLabWebhookNormalizer
from gca.integrations.webhooks import WebhookContext, WebhookVerificationError


def test_github_issue_webhook_verifies_and_normalizes() -> None:
    body = json.dumps(
        {
            "action": "labeled",
            "label": {"name": "gca-run"},
            "repository": {
                "full_name": "owner/repo",
                "clone_url": "https://github.com/owner/repo.git",
                "default_branch": "main",
            },
            "issue": {
                "number": 42,
                "title": "Fix null metadata",
                "body": "Pipeline returns null.",
            },
        }
    ).encode()
    secret = "webhook-secret"
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    context = WebhookContext(
        provider="github",
        headers={
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "delivery-1",
            "X-Hub-Signature-256": signature,
        },
        body=body,
    )
    normalizer = GitHubWebhookNormalizer()

    normalizer.verify(context, secret)
    spec = normalizer.normalize(context, allowed_projects=frozenset({"owner/repo"}))

    assert normalizer.delivery_id(context) == "delivery-1"
    assert spec is not None
    assert spec.labels["issue_id"] == "42"
    assert spec.publication is not None and spec.publication.provider == "github"
    assert "untrusted request data" in spec.task


def test_github_webhook_rejects_bad_signature() -> None:
    context = WebhookContext(
        provider="github",
        headers={"X-Hub-Signature-256": "sha256=bad"},
        body=b"{}",
    )

    with pytest.raises(WebhookVerificationError):
        GitHubWebhookNormalizer().verify(context, "secret")


def test_github_issue_requires_explicit_trigger_label() -> None:
    context = WebhookContext(
        provider="github",
        headers={"X-GitHub-Event": "issues"},
        body=json.dumps({"action": "opened"}).encode(),
    )

    assert GitHubWebhookNormalizer().normalize(context) is None


def test_gitlab_issue_webhook_verifies_and_normalizes() -> None:
    body = json.dumps(
        {
            "object_kind": "issue",
            "project": {
                "path_with_namespace": "group/repo",
                "git_http_url": "https://gitlab.com/group/repo.git",
                "default_branch": "main",
            },
            "object_attributes": {
                "action": "update",
                "iid": 7,
                "title": "Repair pipeline",
                "description": "A stage fails.",
            },
            "changes": {"labels": {"previous": [], "current": [{"title": "gca-run"}]}},
            "labels": [{"title": "gca-run"}],
        }
    ).encode()
    context = WebhookContext(
        provider="gitlab",
        headers={
            "X-Gitlab-Event": "Issue Hook",
            "X-Gitlab-Event-UUID": "delivery-2",
            "X-Gitlab-Token": "shared-token",
        },
        body=body,
    )
    normalizer = GitLabWebhookNormalizer()

    normalizer.verify(context, "shared-token")
    spec = normalizer.normalize(context, allowed_projects=frozenset({"group/repo"}))

    assert normalizer.delivery_id(context) == "delivery-2"
    assert spec is not None
    assert spec.labels["issue_id"] == "7"
    assert spec.publication is not None and spec.publication.provider == "gitlab"
