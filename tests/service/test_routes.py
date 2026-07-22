from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from starlette.testclient import TestClient

from gca_service.app import create_app
from gca_service.config import ServiceSettings


def _settings(tmp_path: Path) -> ServiceSettings:
    return ServiceSettings(
        data_dir=tmp_path / "service",
        api_token="api-token",
        allowed_repository_hosts=frozenset({"example.test", "github.com"}),
        allowed_github_projects=frozenset({"owner/repo"}),
        github_webhook_secret="webhook-secret",
    )


def _run_payload() -> dict[str, object]:
    return {
        "task": "Fix a typo",
        "repository": {
            "url": "https://example.test/owner/repo.git",
            "ref": "main",
        },
        "workflow": "fast",
    }


def test_runs_require_auth_and_are_idempotent(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))

    assert client.post("/runs", json=_run_payload()).status_code == 401
    headers = {"Authorization": "Bearer api-token", "Idempotency-Key": "request-1"}
    first = client.post("/runs", json=_run_payload(), headers=headers)
    replay = client.post("/runs", json=_run_payload(), headers=headers)

    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.json()["id"] == first.json()["id"]
    status = client.get(f"/runs/{first.json()['id']}", headers=headers)
    assert status.status_code == 200
    assert status.json()["status"] == "queued"


def test_run_can_be_cancelled_before_claim(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    headers = {"Authorization": "Bearer api-token"}
    created = client.post("/runs", json=_run_payload(), headers=headers).json()

    response = client.post(f"/runs/{created['id']}/cancel", headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_run_rejects_unallowlisted_repository(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    payload = _run_payload()
    payload["repository"] = {"url": "https://evil.test/repo.git", "ref": "main"}

    response = client.post(
        "/runs",
        json=payload,
        headers={"Authorization": "Bearer api-token"},
    )

    assert response.status_code == 400
    assert "not allowed" in response.json()["error"]


def test_github_webhook_is_verified_and_deduplicated(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    body = json.dumps(
        {
            "action": "opened",
            "repository": {
                "full_name": "owner/repo",
                "clone_url": "https://github.com/owner/repo.git",
                "default_branch": "main",
            },
            "issue": {"number": 5, "title": "Fix issue", "body": "Details"},
        }
    ).encode()
    signature = "sha256=" + hmac.new(
        b"webhook-secret",
        body,
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": "delivery-1",
        "X-Hub-Signature-256": signature,
        "Content-Type": "application/json",
    }

    first = client.post("/webhooks/github", content=body, headers=headers)
    replay = client.post("/webhooks/github", content=body, headers=headers)

    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.json()["id"] == first.json()["id"]
    forged_headers = {**headers, "X-Hub-Signature-256": "sha256=bad"}
    assert client.post("/webhooks/github", content=body, headers=forged_headers).status_code == 401


def test_health_and_readiness(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ready"}
