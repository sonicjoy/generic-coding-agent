"""GitHub webhook normalization and pull-request publication."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from gca.integrations.git_auth import push_with_token
from gca.integrations.http import request_json
from gca.integrations.repository import repository_path
from gca.integrations.scm import ChangeRequest, PublicationError
from gca.integrations.webhooks import (
    WebhookContext,
    WebhookPayloadError,
    WebhookVerificationError,
    issue_task,
    pull_request_review_task,
)
from gca.jobs.models import PublicationTarget, RepositorySpec, RunSpec
from gca.workspace.prepare import repository_host

AGENT_FIX_COMMAND = "/agent fix"


class GitHubScmAdapter:
    """Publish branches and idempotent pull requests through GitHub."""

    provider = "github"

    def __init__(
        self,
        token: str,
        *,
        api_url: str = "https://api.github.com",
        git_host: str = "github.com",
    ) -> None:
        if not token:
            raise ValueError("GitHub token must not be empty")
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.git_host = git_host.lower()

    def supports_repository(self, repository_url: str) -> bool:
        return (
            urlparse(repository_url).scheme == "https"
            and repository_host(repository_url) == self.git_host
        )

    def push(self, workspace: Path, branch: str, repository_url: str) -> None:
        push_with_token(
            workspace,
            branch,
            repository_url=repository_url,
            username="x-access-token",
            token=self.token,
        )

    def open_change_request(self, request: ChangeRequest) -> str:
        slug = repository_path(request.repository_url)
        parts = slug.split("/")
        if len(parts) != 2:
            raise PublicationError(f"invalid GitHub repository path: {slug}")
        owner = parts[0]
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        query = urlencode(
            {
                "state": "open",
                "head": f"{owner}:{request.source_branch}",
                "base": request.target_branch,
            }
        )
        existing = request_json(
            "GET",
            f"{self.api_url}/repos/{slug}/pulls?{query}",
            headers=headers,
        )
        if isinstance(existing, list) and existing:
            return str(existing[0]["html_url"])
        created = request_json(
            "POST",
            f"{self.api_url}/repos/{slug}/pulls",
            headers=headers,
            body={
                "title": request.title,
                "head": request.source_branch,
                "base": request.target_branch,
                "body": request.body,
                "draft": request.draft,
            },
        )
        if not isinstance(created, dict) or not created.get("html_url"):
            raise PublicationError("GitHub pull-request response did not include html_url")
        return str(created["html_url"])


class GitHubWebhookNormalizer:
    """Verify GitHub deliveries and produce generic run specs.

    Supported enqueue triggers:
    - ``issues`` + ``labeled`` with the configured trigger label (default ``gca-run``)
    - ``pull_request_review`` submitted as ``changes_requested``, or with ``/agent fix``
    - ``pull_request_review_comment`` created with ``/agent fix`` in the comment body
    """

    provider = "github"

    def __init__(self, *, trigger_label: str = "gca-run") -> None:
        if not trigger_label.strip():
            raise ValueError("GitHub trigger label must not be empty")
        self.trigger_label = trigger_label

    def verify(self, context: WebhookContext, secret: str) -> None:
        signature = context.header("X-Hub-Signature-256")
        if not secret or not signature.startswith("sha256="):
            raise WebhookVerificationError("missing GitHub webhook signature")
        expected = "sha256=" + hmac.new(secret.encode(), context.body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise WebhookVerificationError("invalid GitHub webhook signature")

    def delivery_id(self, context: WebhookContext) -> str:
        delivery = context.header("X-GitHub-Delivery")
        if not delivery:
            raise WebhookPayloadError("missing X-GitHub-Delivery")
        return delivery

    def normalize(
        self,
        context: WebhookContext,
        *,
        allowed_projects: frozenset[str] = frozenset(),
    ) -> RunSpec | None:
        event = context.header("X-GitHub-Event")
        try:
            payload = json.loads(context.body)
        except json.JSONDecodeError as exc:
            raise WebhookPayloadError(f"invalid GitHub JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise WebhookPayloadError("GitHub payload must be an object")
        if event == "issues":
            return self._normalize_issue_label(payload, allowed_projects=allowed_projects)
        if event == "pull_request_review":
            return self._normalize_pull_request_review(payload, allowed_projects=allowed_projects)
        if event == "pull_request_review_comment":
            return self._normalize_pull_request_review_comment(
                payload, allowed_projects=allowed_projects
            )
        return None

    def _normalize_issue_label(
        self,
        payload: dict[str, Any],
        *,
        allowed_projects: frozenset[str],
    ) -> RunSpec | None:
        if payload.get("action") != "labeled":
            return None
        label = payload.get("label")
        if not isinstance(label, dict) or label.get("name") != self.trigger_label:
            return None
        repository = payload.get("repository")
        issue = payload.get("issue")
        if not isinstance(repository, dict) or not isinstance(issue, dict):
            raise WebhookPayloadError("GitHub issue payload is missing repository or issue")
        project, clone_url, default_branch = self._repository_fields(
            repository, allowed_projects=allowed_projects
        )
        title = str(issue.get("title", ""))
        if not title:
            raise WebhookPayloadError("GitHub issue payload is missing title")
        issue_number = str(issue.get("number", ""))
        return RunSpec(
            task=issue_task(title, str(issue.get("body") or "")),
            repository=RepositorySpec(url=clone_url, ref=default_branch),
            publication=PublicationTarget(
                provider="github",
                base_ref=default_branch,
            ),
            labels={
                "provider": "github",
                "project": project,
                "issue_id": issue_number,
                "issue_title": title,
                "source": "issues.labeled",
            },
        )

    def _normalize_pull_request_review(
        self,
        payload: dict[str, Any],
        *,
        allowed_projects: frozenset[str],
    ) -> RunSpec | None:
        if payload.get("action") != "submitted":
            return None
        review = payload.get("review")
        if not isinstance(review, dict):
            raise WebhookPayloadError("GitHub pull_request_review payload is missing review")
        body = str(review.get("body") or "")
        state = str(review.get("state") or "").lower()
        if state != "changes_requested" and not _has_agent_fix(body):
            return None
        feedback = body.strip() or f"Review state: {state or 'unknown'}"
        return self._run_spec_from_pull_request(
            payload,
            allowed_projects=allowed_projects,
            feedback=feedback,
            source=f"pull_request_review.{state or 'submitted'}",
        )

    def _normalize_pull_request_review_comment(
        self,
        payload: dict[str, Any],
        *,
        allowed_projects: frozenset[str],
    ) -> RunSpec | None:
        if payload.get("action") != "created":
            return None
        comment = payload.get("comment")
        if not isinstance(comment, dict):
            raise WebhookPayloadError(
                "GitHub pull_request_review_comment payload is missing comment"
            )
        body = str(comment.get("body") or "")
        if not _has_agent_fix(body):
            return None
        path = str(comment.get("path") or "").strip()
        line = comment.get("line") or comment.get("original_line")
        location = f"{path}:{line}" if path and line is not None else path
        feedback = body.strip()
        if location:
            feedback = f"Comment on `{location}`:\n{feedback}"
        return self._run_spec_from_pull_request(
            payload,
            allowed_projects=allowed_projects,
            feedback=feedback,
            source="pull_request_review_comment",
        )

    def _run_spec_from_pull_request(
        self,
        payload: dict[str, Any],
        *,
        allowed_projects: frozenset[str],
        feedback: str,
        source: str,
    ) -> RunSpec:
        repository = payload.get("repository")
        pull_request = payload.get("pull_request")
        if not isinstance(repository, dict) or not isinstance(pull_request, dict):
            raise WebhookPayloadError(
                "GitHub pull-request payload is missing repository or pull_request"
            )
        project, clone_url, default_branch = self._repository_fields(
            repository, allowed_projects=allowed_projects
        )
        head = pull_request.get("head")
        base = pull_request.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise WebhookPayloadError("GitHub pull_request is missing head or base")
        head_ref = str(head.get("ref") or "").strip()
        base_ref = str(base.get("ref") or default_branch).strip() or default_branch
        title = str(pull_request.get("title") or "").strip()
        pr_number = str(pull_request.get("number") or "").strip()
        if not head_ref or not title or not pr_number:
            raise WebhookPayloadError(
                "GitHub pull_request payload is missing number, title, or head.ref"
            )
        return RunSpec(
            task=pull_request_review_task(
                title=title,
                pr_number=pr_number,
                head_ref=head_ref,
                feedback=feedback,
            ),
            repository=RepositorySpec(url=clone_url, ref=head_ref),
            publication=PublicationTarget(
                provider="github",
                base_ref=base_ref,
            ),
            labels={
                "provider": "github",
                "project": project,
                "issue_id": pr_number,
                "pr_id": pr_number,
                "issue_title": title[:80],
                "source": source,
                "head_ref": head_ref,
                "base_ref": base_ref,
            },
        )

    def _repository_fields(
        self,
        repository: dict[str, Any],
        *,
        allowed_projects: frozenset[str],
    ) -> tuple[str, str, str]:
        project = str(repository.get("full_name", ""))
        if allowed_projects and project not in allowed_projects:
            raise WebhookVerificationError(f"GitHub project is not allowed: {project}")
        clone_url = str(repository.get("clone_url", ""))
        default_branch = str(repository.get("default_branch", "main")) or "main"
        if not clone_url:
            raise WebhookPayloadError("GitHub payload is missing clone URL")
        return project, clone_url, default_branch


def _has_agent_fix(text: str) -> bool:
    """Return True when a line starts with the hosted remediation command."""

    for line in text.splitlines():
        if line.strip().startswith(AGENT_FIX_COMMAND):
            return True
    return False
