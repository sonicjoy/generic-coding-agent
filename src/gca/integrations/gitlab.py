"""GitLab webhook normalization and merge-request publication."""

from __future__ import annotations

import hmac
import json
from pathlib import Path
from urllib.parse import quote, urlencode

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


class GitLabScmAdapter:
    """Publish branches and idempotent merge requests through GitLab."""

    provider = "gitlab"

    def __init__(
        self,
        token: str,
        *,
        api_url: str = "https://gitlab.com/api/v4",
    ) -> None:
        if not token:
            raise ValueError("GitLab token must not be empty")
        self.token = token
        self.api_url = api_url.rstrip("/")

    def push(self, workspace: Path, branch: str) -> None:
        push_with_token(workspace, branch, username="oauth2", token=self.token)

    def open_change_request(self, request: ChangeRequest) -> str:
        project = quote(repository_path(request.repository_url), safe="")
        headers = {"PRIVATE-TOKEN": self.token}
        query = urlencode(
            {
                "state": "opened",
                "source_branch": request.source_branch,
                "target_branch": request.target_branch,
            }
        )
        existing = request_json(
            "GET",
            f"{self.api_url}/projects/{project}/merge_requests?{query}",
            headers=headers,
        )
        if isinstance(existing, list) and existing:
            return str(existing[0]["web_url"])
        title = f"Draft: {request.title}" if request.draft else request.title
        created = request_json(
            "POST",
            f"{self.api_url}/projects/{project}/merge_requests",
            headers=headers,
            body={
                "source_branch": request.source_branch,
                "target_branch": request.target_branch,
                "title": title,
                "description": request.body,
            },
        )
        if not isinstance(created, dict) or not created.get("web_url"):
            raise PublicationError("GitLab merge-request response did not include web_url")
        return str(created["web_url"])


class GitLabWebhookNormalizer:
    """Verify GitLab issue deliveries and produce generic run specs."""

    provider = "gitlab"

    def verify(self, context: WebhookContext, secret: str) -> None:
        supplied = context.header("X-Gitlab-Token")
        if not secret or not supplied or not hmac.compare_digest(supplied, secret):
            raise WebhookVerificationError("invalid GitLab webhook token")

    def delivery_id(self, context: WebhookContext) -> str:
        delivery = context.header("X-Gitlab-Event-UUID") or context.header(
            "X-Gitlab-Webhook-UUID"
        )
        if not delivery:
            raise WebhookPayloadError("missing GitLab delivery UUID")
        return delivery

    def normalize(
        self,
        context: WebhookContext,
        *,
        allowed_projects: frozenset[str] = frozenset(),
    ) -> RunSpec | None:
        if context.header("X-Gitlab-Event") != "Issue Hook":
            return None
        try:
            payload = json.loads(context.body)
        except json.JSONDecodeError as exc:
            raise WebhookPayloadError(f"invalid GitLab JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise WebhookPayloadError("GitLab payload must be an object")
        if payload.get("object_kind") != "issue":
            return None
        project = payload.get("project")
        attributes = payload.get("object_attributes")
        if not isinstance(project, dict) or not isinstance(attributes, dict):
            raise WebhookPayloadError("GitLab issue payload is missing project or attributes")
        if attributes.get("action") not in {"open", "reopen"}:
            return None
        project_path = str(project.get("path_with_namespace", ""))
        if allowed_projects and project_path not in allowed_projects:
            raise WebhookVerificationError(f"GitLab project is not allowed: {project_path}")
        clone_url = str(project.get("git_http_url", ""))
        default_branch = str(project.get("default_branch", "main"))
        title = str(attributes.get("title", ""))
        if not clone_url or not title:
            raise WebhookPayloadError("GitLab issue payload is missing clone URL or title")
        issue_id = str(attributes.get("iid", ""))
        return RunSpec(
            task=issue_task(title, str(attributes.get("description") or "")),
            repository=RepositorySpec(url=clone_url, ref=default_branch),
            publication=PublicationTarget(
                provider="gitlab",
                base_ref=default_branch,
            ),
            labels={
                "provider": "gitlab",
                "project": project_path,
                "issue_id": issue_id,
            },
        )
