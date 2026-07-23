"""Normalized GitLab webhook events for durable issue-session ingestion."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from gca.integrations.webhook_registration import (
    WebhookRegistration,
    verify_gitlab_signature,
)
from gca.integrations.webhooks import (
    WebhookContext,
    WebhookPayloadError,
    WebhookVerificationError,
)

AGENT_COMMANDS = frozenset({"/agent run", "/agent fix", "/agent cancel", "/agent status"})


@dataclass(frozen=True)
class NormalizedGitLabEvent:
    """Provider-normalized GitLab delivery used by the issue-session layer."""

    delivery_id: str
    event_uuid: str
    event_type: str
    action: str
    object_key: str
    gitlab_instance: str
    project_id: int
    project_path: str
    repository_url: str
    issue_iid: int | None = None
    issue_title: str = ""
    issue_description: str = ""
    mr_iid: int | None = None
    note_id: int | None = None
    note_body: str = ""
    note_system: bool = False
    actor_id: int | None = None
    actor_username: str = ""
    command: str | None = None
    labels: frozenset[str] = frozenset()
    label_added: bool = False
    pipeline_id: int | None = None
    pipeline_status: str = ""
    pipeline_sha: str = ""
    mr_state: str = ""
    target_branch: str = ""
    source_branch: str = ""
    relevant: bool = True
    ignore_reason: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


class GitLabIssueEventNormalizer:
    """Verify and normalize GitLab issue, note, MR, and pipeline deliveries."""

    provider = "gitlab"

    def __init__(self, registration: WebhookRegistration) -> None:
        self.registration = registration

    def verify(self, context: WebhookContext) -> None:
        """Verify registration-bound webhook authenticity."""

        verify_gitlab_signature(
            headers=context.headers,
            body=context.body,
            registration=self.registration,
        )
        instance = context.header("X-Gitlab-Instance").rstrip("/")
        if instance and instance != self.registration.gitlab_instance.rstrip("/"):
            raise WebhookVerificationError("GitLab instance does not match registration")

    def delivery_id(self, context: WebhookContext) -> str:
        """Return the durable retry identity for this delivery."""

        delivery = (
            context.header("webhook-id")
            or context.header("Idempotency-Key")
            or context.header("X-Gitlab-Event-UUID")
        )
        if not delivery:
            raise WebhookPayloadError("missing GitLab webhook delivery identity")
        return delivery

    def normalize(self, context: WebhookContext) -> NormalizedGitLabEvent:
        """Normalize one verified delivery into a durable event."""

        event_type = context.header("X-Gitlab-Event")
        if event_type not in self.registration.enabled_events:
            raise WebhookPayloadError(f"event type is not enabled: {event_type}")
        try:
            payload = json.loads(context.body)
        except json.JSONDecodeError as exc:
            raise WebhookPayloadError(f"invalid GitLab JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise WebhookPayloadError("GitLab payload must be an object")
        project = payload.get("project")
        if not isinstance(project, dict):
            raise WebhookPayloadError("GitLab payload is missing project")
        project_id = project.get("id")
        if not isinstance(project_id, int) or project_id != self.registration.project_id:
            raise WebhookVerificationError("GitLab project id does not match registration")
        project_path = str(project.get("path_with_namespace", ""))
        if project_path and project_path != self.registration.project_path:
            raise WebhookVerificationError("GitLab project path does not match registration")
        repository_url = str(
            project.get("git_http_url")
            or self.registration.repository_url
            or f"https://{urlparse(self.registration.gitlab_instance).hostname}/{project_path}.git"
        )
        delivery_id = self.delivery_id(context)
        event_uuid = context.header("X-Gitlab-Event-UUID") or delivery_id
        raw_user = payload.get("user")
        user: dict[str, Any] = raw_user if isinstance(raw_user, dict) else {}
        actor_id = user.get("id") if isinstance(user.get("id"), int) else None
        actor_username = str(user.get("username", ""))
        if event_type == "Issue Hook":
            return self._normalize_issue(
                payload,
                delivery_id=delivery_id,
                event_uuid=event_uuid,
                project_id=project_id,
                project_path=project_path,
                repository_url=repository_url,
                actor_id=actor_id,
                actor_username=actor_username,
            )
        if event_type == "Note Hook":
            return self._normalize_note(
                payload,
                delivery_id=delivery_id,
                event_uuid=event_uuid,
                project_id=project_id,
                project_path=project_path,
                repository_url=repository_url,
                actor_id=actor_id,
                actor_username=actor_username,
            )
        if event_type == "Merge Request Hook":
            return self._normalize_merge_request(
                payload,
                delivery_id=delivery_id,
                event_uuid=event_uuid,
                project_id=project_id,
                project_path=project_path,
                repository_url=repository_url,
                actor_id=actor_id,
                actor_username=actor_username,
            )
        if event_type == "Pipeline Hook":
            return self._normalize_pipeline(
                payload,
                delivery_id=delivery_id,
                event_uuid=event_uuid,
                project_id=project_id,
                project_path=project_path,
                repository_url=repository_url,
                actor_id=actor_id,
                actor_username=actor_username,
            )
        raise WebhookPayloadError(f"unsupported GitLab event type: {event_type}")

    def _normalize_issue(
        self,
        payload: dict[str, Any],
        *,
        delivery_id: str,
        event_uuid: str,
        project_id: int,
        project_path: str,
        repository_url: str,
        actor_id: int | None,
        actor_username: str,
    ) -> NormalizedGitLabEvent:
        attributes = payload.get("object_attributes")
        if not isinstance(attributes, dict):
            raise WebhookPayloadError("GitLab issue payload is missing attributes")
        action = str(attributes.get("action", ""))
        issue_iid = attributes.get("iid")
        if not isinstance(issue_iid, int):
            raise WebhookPayloadError("GitLab issue payload is missing iid")
        labels = _label_titles(payload.get("labels"))
        label_added = False
        relevant = False
        ignore_reason = ""
        if action == "open" and self.registration.trigger_label in labels:
            relevant = True
            label_added = True
        elif action == "update":
            changes = payload.get("changes")
            if isinstance(changes, dict) and "labels" in changes:
                previous = {
                    str(item.get("title", ""))
                    for item in ((changes.get("labels") or {}).get("previous") or [])
                    if isinstance(item, dict)
                }
                current = {
                    str(item.get("title", ""))
                    for item in ((changes.get("labels") or {}).get("current") or labels)
                    if isinstance(item, dict)
                } or labels
                if (
                    self.registration.trigger_label not in previous
                    and self.registration.trigger_label in current
                ):
                    relevant = True
                    label_added = True
                    labels = frozenset(current)
            if not relevant and action in {"close", "reopen"}:
                relevant = True
        elif action in {"close", "reopen"}:
            relevant = True
        else:
            ignore_reason = "issue event is not a start or lifecycle change"
        return NormalizedGitLabEvent(
            delivery_id=delivery_id,
            event_uuid=event_uuid,
            event_type="Issue Hook",
            action=action,
            object_key=f"issue:{issue_iid}:{action}:{int(label_added)}",
            gitlab_instance=self.registration.gitlab_instance,
            project_id=project_id,
            project_path=project_path,
            repository_url=repository_url,
            issue_iid=issue_iid,
            issue_title=str(attributes.get("title", "")),
            issue_description=str(attributes.get("description") or ""),
            actor_id=actor_id,
            actor_username=actor_username,
            labels=labels,
            label_added=label_added,
            relevant=relevant,
            ignore_reason=ignore_reason,
            target_branch=str(payload.get("project", {}).get("default_branch", "main")),
            payload=_safe_payload(payload),
        )

    def _normalize_note(
        self,
        payload: dict[str, Any],
        *,
        delivery_id: str,
        event_uuid: str,
        project_id: int,
        project_path: str,
        repository_url: str,
        actor_id: int | None,
        actor_username: str,
    ) -> NormalizedGitLabEvent:
        attributes = payload.get("object_attributes")
        if not isinstance(attributes, dict):
            raise WebhookPayloadError("GitLab note payload is missing attributes")
        action = str(attributes.get("action", "create"))
        note_id = attributes.get("id")
        if not isinstance(note_id, int):
            raise WebhookPayloadError("GitLab note payload is missing id")
        noteable_type = str(attributes.get("noteable_type", ""))
        body = str(attributes.get("note") or "")
        system = bool(attributes.get("system", False))
        command = _exact_command(body) if action == "create" and not system else None
        issue_iid = None
        mr_iid = None
        issue_title = ""
        issue_description = ""
        if noteable_type == "Issue":
            issue = payload.get("issue")
            if not isinstance(issue, dict) or not isinstance(issue.get("iid"), int):
                raise WebhookPayloadError("GitLab issue note is missing issue iid")
            issue_iid = int(issue["iid"])
            issue_title = str(issue.get("title", ""))
            issue_description = str(issue.get("description") or "")
        elif noteable_type == "MergeRequest":
            merge_request = payload.get("merge_request")
            if not isinstance(merge_request, dict) or not isinstance(merge_request.get("iid"), int):
                raise WebhookPayloadError("GitLab MR note is missing merge request iid")
            mr_iid = int(merge_request["iid"])
        else:
            return NormalizedGitLabEvent(
                delivery_id=delivery_id,
                event_uuid=event_uuid,
                event_type="Note Hook",
                action=action,
                object_key=f"note:{note_id}:{action}",
                gitlab_instance=self.registration.gitlab_instance,
                project_id=project_id,
                project_path=project_path,
                repository_url=repository_url,
                note_id=note_id,
                note_body=body,
                note_system=system,
                actor_id=actor_id,
                actor_username=actor_username,
                relevant=False,
                ignore_reason=f"unsupported noteable type: {noteable_type}",
                payload=_safe_payload(payload),
            )
        relevant = True
        ignore_reason = ""
        if system:
            relevant = False
            ignore_reason = "system note"
        elif (
            self.registration.bot_user_id is not None and actor_id == self.registration.bot_user_id
        ):
            relevant = False
            ignore_reason = "bot-authored note"
        elif action != "create":
            relevant = False
            ignore_reason = "note edit does not start work"
        return NormalizedGitLabEvent(
            delivery_id=delivery_id,
            event_uuid=event_uuid,
            event_type="Note Hook",
            action=action,
            object_key=f"note:{note_id}:{action}",
            gitlab_instance=self.registration.gitlab_instance,
            project_id=project_id,
            project_path=project_path,
            repository_url=repository_url,
            issue_iid=issue_iid,
            issue_title=issue_title,
            issue_description=issue_description,
            mr_iid=mr_iid,
            note_id=note_id,
            note_body=body,
            note_system=system,
            actor_id=actor_id,
            actor_username=actor_username,
            command=command,
            relevant=relevant,
            ignore_reason=ignore_reason,
            payload=_safe_payload(payload),
        )

    def _normalize_merge_request(
        self,
        payload: dict[str, Any],
        *,
        delivery_id: str,
        event_uuid: str,
        project_id: int,
        project_path: str,
        repository_url: str,
        actor_id: int | None,
        actor_username: str,
    ) -> NormalizedGitLabEvent:
        attributes = payload.get("object_attributes")
        if not isinstance(attributes, dict):
            raise WebhookPayloadError("GitLab merge request payload is missing attributes")
        action = str(attributes.get("action", ""))
        mr_iid = attributes.get("iid")
        if not isinstance(mr_iid, int):
            raise WebhookPayloadError("GitLab merge request payload is missing iid")
        relevant = action in {"merge", "close", "reopen", "update", "approved", "unapproved"}
        return NormalizedGitLabEvent(
            delivery_id=delivery_id,
            event_uuid=event_uuid,
            event_type="Merge Request Hook",
            action=action,
            object_key=f"mr:{mr_iid}:{action}:{attributes.get('updated_at', '')}",
            gitlab_instance=self.registration.gitlab_instance,
            project_id=project_id,
            project_path=project_path,
            repository_url=repository_url,
            mr_iid=mr_iid,
            actor_id=actor_id,
            actor_username=actor_username,
            mr_state=str(attributes.get("state", "")),
            target_branch=str(attributes.get("target_branch", "")),
            source_branch=str(attributes.get("source_branch", "")),
            pipeline_sha=str(attributes.get("last_commit", {}).get("id", ""))
            if isinstance(attributes.get("last_commit"), dict)
            else "",
            relevant=relevant,
            ignore_reason="" if relevant else "unsupported merge request action",
            payload=_safe_payload(payload),
        )

    def _normalize_pipeline(
        self,
        payload: dict[str, Any],
        *,
        delivery_id: str,
        event_uuid: str,
        project_id: int,
        project_path: str,
        repository_url: str,
        actor_id: int | None,
        actor_username: str,
    ) -> NormalizedGitLabEvent:
        attributes = payload.get("object_attributes")
        if not isinstance(attributes, dict):
            raise WebhookPayloadError("GitLab pipeline payload is missing attributes")
        pipeline_id = attributes.get("id")
        if not isinstance(pipeline_id, int):
            raise WebhookPayloadError("GitLab pipeline payload is missing id")
        merge_request = payload.get("merge_request")
        mr_iid = None
        if isinstance(merge_request, dict) and isinstance(merge_request.get("iid"), int):
            mr_iid = int(merge_request["iid"])
        status = str(attributes.get("status", ""))
        relevant = mr_iid is not None and status in {
            "success",
            "failed",
            "canceled",
            "manual",
        }
        return NormalizedGitLabEvent(
            delivery_id=delivery_id,
            event_uuid=event_uuid,
            event_type="Pipeline Hook",
            action=status,
            object_key=f"pipeline:{pipeline_id}:{status}",
            gitlab_instance=self.registration.gitlab_instance,
            project_id=project_id,
            project_path=project_path,
            repository_url=repository_url,
            mr_iid=mr_iid,
            actor_id=actor_id,
            actor_username=actor_username,
            pipeline_id=pipeline_id,
            pipeline_status=status,
            pipeline_sha=str(attributes.get("sha", "")),
            relevant=relevant,
            ignore_reason="" if relevant else "pipeline is unrelated or non-actionable",
            payload=_safe_payload(payload),
        )


def _exact_command(body: str) -> str | None:
    trimmed = body.strip()
    if trimmed in AGENT_COMMANDS:
        return trimmed
    return None


def _label_titles(value: object) -> frozenset[str]:
    if not isinstance(value, list):
        return frozenset()
    return frozenset(
        str(item.get("title", "")) for item in value if isinstance(item, dict) and item.get("title")
    )


def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist only bounded metadata fields from a webhook payload."""

    allowed = {
        "object_kind",
        "event_type",
        "user",
        "project",
        "object_attributes",
        "labels",
        "changes",
        "issue",
        "merge_request",
    }
    result: dict[str, Any] = {}
    for key in allowed:
        if key in payload:
            result[key] = payload[key]
    encoded = json.dumps(result, default=str)
    if len(encoded) > 20_000:
        return {"truncated": True, "object_kind": payload.get("object_kind")}
    return result


def neutralize_untrusted_markdown(text: str) -> str:
    """Neutralize GitLab quick actions and mass mentions in untrusted text."""

    lines = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("/"):
            lines.append(re.sub(r"^(\s*)/", r"\1\\/", line))
        else:
            lines.append(line.replace("@", "@\u200b"))
    return "\n".join(lines)
