from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from gca.integrations.github import GitHubWebhookNormalizer
from gca.integrations.gitlab import GitLabWebhookNormalizer
from gca.integrations.webhooks import WebhookContext, WebhookVerificationError


def _github_context(event: str, payload: dict, *, delivery: str = "delivery-1") -> WebhookContext:
    body = json.dumps(payload).encode()
    secret = "webhook-secret"
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return WebhookContext(
        provider="github",
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": signature,
        },
        body=body,
    )


def _pull_request_payload(**overrides: object) -> dict:
    payload: dict = {
        "repository": {
            "full_name": "owner/repo",
            "clone_url": "https://github.com/owner/repo.git",
            "default_branch": "main",
        },
        "pull_request": {
            "number": 43,
            "title": "Fix budget pause publish gaps",
            "head": {"ref": "gca/3c3a8de414bf"},
            "base": {"ref": "main"},
        },
    }
    payload.update(overrides)
    return payload


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
    assert spec.labels["issue_title"] == "Fix null metadata"
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


def test_github_pull_request_review_changes_requested_enqueues_run() -> None:
    context = _github_context(
        "pull_request_review",
        _pull_request_payload(
            action="submitted",
            review={
                "state": "changes_requested",
                "body": "Please add draft-publish on review budget pause.",
            },
        ),
    )
    spec = GitHubWebhookNormalizer().normalize(context, allowed_projects=frozenset({"owner/repo"}))

    assert spec is not None
    assert spec.repository.ref == "gca/3c3a8de414bf"
    assert spec.publication is not None
    assert spec.publication.base_ref == "main"
    assert spec.labels["pr_id"] == "43"
    assert spec.labels["source"] == "pull_request_review.changes_requested"
    assert "draft-publish" in spec.task
    assert "untrusted request data" in spec.task


def test_github_pull_request_review_comment_requires_agent_fix() -> None:
    ignored = _github_context(
        "pull_request_review_comment",
        _pull_request_payload(
            action="created",
            comment={
                "body": "nit: rename this helper",
                "path": "src/gca/orchestrator.py",
                "line": 10,
            },
        ),
        delivery="delivery-ignore",
    )
    assert GitHubWebhookNormalizer().normalize(ignored) is None

    context = _github_context(
        "pull_request_review_comment",
        _pull_request_payload(
            action="created",
            comment={
                "body": "/agent fix\nAddress the GitHub resume-signal gap.",
                "path": "src/gca/issue_sessions/outcomes.py",
                "line": 91,
            },
        ),
        delivery="delivery-fix",
    )
    spec = GitHubWebhookNormalizer().normalize(context, allowed_projects=frozenset({"owner/repo"}))

    assert spec is not None
    assert spec.repository.ref == "gca/3c3a8de414bf"
    assert spec.labels["source"] == "pull_request_review_comment"
    assert "/agent fix" in spec.task
    assert "outcomes.py:91" in spec.task


def test_github_pull_request_review_approved_without_agent_fix_is_ignored() -> None:
    context = _github_context(
        "pull_request_review",
        _pull_request_payload(
            action="submitted",
            review={"state": "approved", "body": "LGTM"},
        ),
    )
    assert GitHubWebhookNormalizer().normalize(context) is None


def test_github_merged_pull_request_is_parsed_for_cleanup() -> None:
    context = _github_context(
        "pull_request",
        {
            "action": "closed",
            "repository": {
                "full_name": "owner/repo",
                "clone_url": "https://github.com/owner/repo.git",
                "default_branch": "main",
            },
            "pull_request": {
                "number": 46,
                "merged": True,
                "html_url": "https://github.com/owner/repo/pull/46",
                "head": {"ref": "gca/cdaf54d9fb2a"},
            },
        },
    )
    merged = GitHubWebhookNormalizer().parse_merged_pull_request(
        context, allowed_projects=frozenset({"owner/repo"})
    )
    assert merged is not None
    assert merged.number == "46"
    assert merged.head_ref == "gca/cdaf54d9fb2a"
    assert merged.url.endswith("/pull/46")
    # Merge is lifecycle cleanup, not a new run.
    assert GitHubWebhookNormalizer().normalize(context) is None


def test_github_closed_unmerged_pull_request_is_ignored() -> None:
    context = _github_context(
        "pull_request",
        {
            "action": "closed",
            "repository": {
                "full_name": "owner/repo",
                "clone_url": "https://github.com/owner/repo.git",
                "default_branch": "main",
            },
            "pull_request": {
                "number": 46,
                "merged": False,
                "html_url": "https://github.com/owner/repo/pull/46",
                "head": {"ref": "gca/cdaf54d9fb2a"},
            },
        },
    )
    assert (
        GitHubWebhookNormalizer().parse_merged_pull_request(
            context, allowed_projects=frozenset({"owner/repo"})
        )
        is None
    )


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
    assert spec.labels["issue_title"] == "Repair pipeline"
    assert spec.publication is not None and spec.publication.provider == "gitlab"
