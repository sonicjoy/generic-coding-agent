"""GitHub webhook normalization and pull-request publication."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from urllib.parse import urlencode

from gca.integrations.git_auth import push_with_token
from gca.integrations.http import request_json
from gca.integrations.repository import repository_path
from gca.integrations.scm import ChangeRequest, PublicationError
from gca.integrations.webhooks import (
    WebhookContext,
    WebhookPayloadError,
    WebhookVerificationError,
    issue_task,
)
from gca.jobs.models import PublicationTarget, RepositorySpec, RunSpec


class GitHubScmAdapter:
    """Publish branches and idempotent pull requests through GitHub."""

    provider = "github"

    def __init__(
        self,
        token: str,
        *,
        api_url: str = "https://api.github.com",
    ) -> None:
        if not token:
            raise ValueError("GitHub token must not be empty")
        self.token = token
        self.api_url = api_url.rstrip("/")

    def push(self, workspace: Path, branch: str) -> None:
        push_with_token(workspace, branch, username="x-access-token", token=self.token)

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
    """Verify GitHub issue deliveries and produce generic run specs."""

    provider = "github"

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
        if context.header("X-GitHub-Event") != "issues":
            return None
        try:
            payload = json.loads(context.body)
        except json.JSONDecodeError as exc:
            raise WebhookPayloadError(f"invalid GitHub JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise WebhookPayloadError("GitHub payload must be an object")
        if payload.get("action") != "opened":
            return None
        repository = payload.get("repository")
        issue = payload.get("issue")
        if not isinstance(repository, dict) or not isinstance(issue, dict):
            raise WebhookPayloadError("GitHub issue payload is missing repository or issue")
        project = str(repository.get("full_name", ""))
        if allowed_projects and project not in allowed_projects:
            raise WebhookVerificationError(f"GitHub project is not allowed: {project}")
        clone_url = str(repository.get("clone_url", ""))
        default_branch = str(repository.get("default_branch", "main"))
        title = str(issue.get("title", ""))
        if not clone_url or not title:
            raise WebhookPayloadError("GitHub issue payload is missing clone URL or title")
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
            },
        )
