"""HTTP tests for issue-session APIs and registered GitLab webhooks."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

from starlette.testclient import TestClient

from gca.integrations.webhook_registration import WebhookRegistration
from gca_service.app import create_app
from gca_service.config import ServiceSettings
from gca_service.state import ServiceState


def _settings(tmp_path: Path, registration: WebhookRegistration) -> ServiceSettings:
    return ServiceSettings(
        data_dir=tmp_path,
        api_token="test-api-token-123456",
        allowed_repository_hosts=frozenset({"gitlab.com"}),
        gitlab_webhook_registrations={registration.id: registration},
        membership_access_levels={(registration.project_id, 7): 40},
    )


def _registration() -> WebhookRegistration:
    return WebhookRegistration.from_mapping(
        {
            "id": "reg1prod",
            "gitlab_instance": "https://gitlab.com",
            "project_id": 42,
            "project_path": "group/project",
            "hook_uuid": "hook-1",
            "signing_secret": "super-secret-signing-token",
            "repository_url": "https://gitlab.com/group/project.git",
            "actor_allowlist": [7],
        }
    )


def test_registered_webhook_and_session_apis(tmp_path: Path) -> None:
    registration = _registration()
    state = ServiceState.build(_settings(tmp_path, registration))
    client = TestClient(create_app(state=state))
    body = {
        "object_kind": "issue",
        "user": {"id": 7, "username": "dev"},
        "project": {
            "id": 42,
            "path_with_namespace": "group/project",
            "git_http_url": "https://gitlab.com/group/project.git",
            "default_branch": "main",
        },
        "object_attributes": {
            "action": "open",
            "iid": 3,
            "title": "Alert",
            "description": "null fields",
        },
        "labels": [{"title": "gca-run"}],
    }
    raw = json.dumps(body).encode()
    webhook_id = "wh-1"
    timestamp = str(int(time.time()))
    digest = hmac.new(
        registration.signing_secret.encode(),
        f"{webhook_id}.{timestamp}.".encode() + raw,
        hashlib.sha256,
    ).digest()
    response = client.post(
        "/webhooks/gitlab/reg1prod",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Gitlab-Event": "Issue Hook",
            "X-Gitlab-Instance": "https://gitlab.com",
            "X-Gitlab-Webhook-UUID": "hook-1",
            "X-Gitlab-Event-UUID": "evt-1",
            "webhook-id": webhook_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": "v1," + base64.b64encode(digest).decode(),
        },
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "accepted"
    session_id = payload["issue_session_id"]
    listed = client.get(
        "/issue-sessions",
        headers={"Authorization": "Bearer test-api-token-123456"},
    )
    assert listed.status_code == 200
    assert listed.json()["items"][0]["id"] == session_id
    detail = client.get(
        f"/issue-sessions/{session_id}",
        headers={"Authorization": "Bearer test-api-token-123456"},
    )
    assert detail.status_code == 200
    events = client.get(
        f"/issue-sessions/{session_id}/events",
        headers={"Authorization": "Bearer test-api-token-123456"},
    )
    assert events.status_code == 200
    assert events.json()["items"]
    transcript = client.get(
        f"/issue-sessions/{session_id}/transcript",
        headers={"Authorization": "Bearer test-api-token-123456"},
    )
    assert transcript.status_code == 200
    assert transcript.json()["export_schema_version"] == 1
