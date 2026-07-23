from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from starlette.testclient import TestClient

from gca.jobs.lifecycle import transition_job
from gca.jobs.models import JobStatus, RepositorySpec, RunSpec
from gca_service.app import create_app
from gca_service.config import ServiceSettings
from gca_service.state import ServiceState


def _settings(tmp_path: Path) -> ServiceSettings:
    return ServiceSettings(
        data_dir=tmp_path / "service",
        api_token="api-token-123456",
        allowed_repository_hosts=frozenset({"example.test", "github.com"}),
        allowed_github_projects=frozenset({"owner/repo"}),
        github_webhook_secret="webhook-secret-123456",
        github_token="github-token-for-tests",
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
    headers = {"Authorization": "Bearer api-token-123456", "Idempotency-Key": "request-1"}
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
    headers = {"Authorization": "Bearer api-token-123456"}
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
        headers={"Authorization": "Bearer api-token-123456"},
    )

    assert response.status_code == 400
    assert "not allowed" in response.json()["error"]


def test_github_webhook_is_verified_and_deduplicated(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    body = json.dumps(
        {
            "action": "labeled",
            "label": {"name": "gca-run"},
            "repository": {
                "full_name": "owner/repo",
                "clone_url": "https://github.com/owner/repo.git",
                "default_branch": "main",
            },
            "issue": {"number": 5, "title": "Fix issue", "body": "Details"},
        }
    ).encode()
    signature = (
        "sha256="
        + hmac.new(
            b"webhook-secret-123456",
            body,
            hashlib.sha256,
        ).hexdigest()
    )
    headers = {
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": "delivery-1",
        "X-Hub-Signature-256": signature,
        "Content-Type": "application/json",
    }

    first = client.post("/webhooks/github", content=body, headers=headers)
    replay_headers = {**headers, "X-GitHub-Delivery": "delivery-2"}
    replay = client.post("/webhooks/github", content=body, headers=replay_headers)

    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.json()["id"] == first.json()["id"]
    forged_headers = {**headers, "X-Hub-Signature-256": "sha256=bad"}
    assert client.post("/webhooks/github", content=body, headers=forged_headers).status_code == 401


def test_github_webhook_applies_service_default_max_steps(tmp_path: Path) -> None:
    settings = ServiceSettings(
        data_dir=tmp_path / "service-budget",
        api_token="api-token-123456",
        allowed_repository_hosts=frozenset({"example.test", "github.com"}),
        allowed_github_projects=frozenset({"owner/repo"}),
        github_webhook_secret="webhook-secret-123456",
        github_token="github-token-for-tests",
        default_max_steps=100,
    )
    client = TestClient(create_app(settings))
    body = json.dumps(
        {
            "action": "labeled",
            "label": {"name": "gca-run"},
            "repository": {
                "full_name": "owner/repo",
                "clone_url": "https://github.com/owner/repo.git",
                "default_branch": "main",
            },
            "issue": {"number": 5, "title": "Fix issue", "body": "Details"},
        }
    ).encode()
    signature = (
        "sha256="
        + hmac.new(
            b"webhook-secret-123456",
            body,
            hashlib.sha256,
        ).hexdigest()
    )
    response = client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "delivery-budget",
            "X-Hub-Signature-256": signature,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 202
    assert response.json()["max_steps"] == 100
    job = ServiceState.build(settings).store.load(response.json()["id"])
    assert job.run_spec.max_steps == 100


def test_create_run_applies_service_default_max_steps(tmp_path: Path) -> None:
    settings = ServiceSettings(
        data_dir=tmp_path / "service-run-budget",
        api_token="api-token-123456",
        allowed_repository_hosts=frozenset({"example.test", "github.com"}),
        default_max_steps=80,
    )
    client = TestClient(create_app(settings))
    response = client.post(
        "/runs",
        json=_run_payload(),
        headers={"Authorization": "Bearer api-token-123456"},
    )

    assert response.status_code == 202
    assert response.json()["max_steps"] == 80


def test_create_run_explicit_max_steps_overrides_service_default(tmp_path: Path) -> None:
    settings = ServiceSettings(
        data_dir=tmp_path / "service-run-override",
        api_token="api-token-123456",
        allowed_repository_hosts=frozenset({"example.test", "github.com"}),
        default_max_steps=80,
    )
    client = TestClient(create_app(settings))
    payload = _run_payload()
    payload["max_steps"] = 12
    response = client.post(
        "/runs",
        json=payload,
        headers={"Authorization": "Bearer api-token-123456"},
    )

    assert response.status_code == 202
    assert response.json()["max_steps"] == 12


def test_health_and_readiness(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ready"}


def test_runs_reject_malformed_types_and_oversized_body(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))
    headers = {"Authorization": "Bearer api-token-123456"}
    malformed = _run_payload()
    malformed["labels"] = ["not", "a", "mapping"]

    response = client.post("/runs", json=malformed, headers=headers)

    assert response.status_code == 400
    constrained = ServiceSettings(
        data_dir=tmp_path / "small-service",
        api_token="api-token-123456",
        allowed_repository_hosts=frozenset({"example.test"}),
        max_request_bytes=10,
    )
    small_client = TestClient(create_app(constrained))
    oversized = small_client.post("/runs", content=b"{" + b"x" * 20 + b"}", headers=headers)
    assert oversized.status_code == 413


def test_paused_run_can_resume_with_larger_budget(tmp_path: Path) -> None:
    state = ServiceState.build(_settings(tmp_path))
    job = state.store.create(
        RunSpec(
            task="Continue work",
            repository=RepositorySpec("https://example.test/owner/repo.git"),
            max_steps=5,
        )
    )
    transition_job(job, JobStatus.RUNNING)
    state.store.save(job)
    transition_job(job, JobStatus.PAUSED)
    state.store.save(job)
    client = TestClient(create_app(state=state))

    response = client.post(
        f"/runs/{job.id}/resume",
        json={"max_steps": 10},
        headers={"Authorization": "Bearer api-token-123456"},
    )

    assert response.status_code == 202
    resumed = state.store.load(job.id)
    assert resumed.status == JobStatus.QUEUED
    assert resumed.run_spec.max_steps == 10


def test_create_run_rejects_publication_without_scm_token(tmp_path: Path) -> None:
    settings = ServiceSettings(
        data_dir=tmp_path / "service-no-token",
        api_token="api-token-123456",
        allowed_repository_hosts=frozenset({"example.test", "github.com"}),
    )
    client = TestClient(create_app(settings))
    payload = _run_payload()
    payload["publication"] = {"provider": "github", "base_ref": "main"}

    response = client.post(
        "/runs",
        json=payload,
        headers={"Authorization": "Bearer api-token-123456"},
    )

    assert response.status_code == 400
    assert "GCA_GITHUB_TOKEN" in response.json()["error"]


def test_create_run_strips_publication_when_publish_mode_off(tmp_path: Path) -> None:
    settings = ServiceSettings(
        data_dir=tmp_path / "service-publish-off",
        api_token="api-token-123456",
        allowed_repository_hosts=frozenset({"example.test", "github.com"}),
        publish_mode="off",
    )
    client = TestClient(create_app(settings))
    payload = _run_payload()
    payload["publication"] = {"provider": "github", "base_ref": "main"}

    response = client.post(
        "/runs",
        json=payload,
        headers={"Authorization": "Bearer api-token-123456"},
    )

    assert response.status_code == 202
    job = ServiceState.build(settings).store.load(response.json()["id"])
    assert job.run_spec.publication is None


def test_github_webhook_rejects_publication_without_scm_token(tmp_path: Path) -> None:
    settings = ServiceSettings(
        data_dir=tmp_path / "service-webhook-no-token",
        api_token="api-token-123456",
        allowed_repository_hosts=frozenset({"example.test", "github.com"}),
        allowed_github_projects=frozenset({"owner/repo"}),
        github_webhook_secret="webhook-secret-123456",
    )
    client = TestClient(create_app(settings))
    body = json.dumps(
        {
            "action": "labeled",
            "label": {"name": "gca-run"},
            "repository": {
                "full_name": "owner/repo",
                "clone_url": "https://github.com/owner/repo.git",
                "default_branch": "main",
            },
            "issue": {"number": 5, "title": "Fix issue", "body": "Details"},
        }
    ).encode()
    signature = (
        "sha256="
        + hmac.new(
            b"webhook-secret-123456",
            body,
            hashlib.sha256,
        ).hexdigest()
    )

    response = client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "delivery-no-token",
            "X-Hub-Signature-256": signature,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 400
    assert "GCA_GITHUB_TOKEN" in response.json()["error"]
