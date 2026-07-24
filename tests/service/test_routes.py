from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from starlette.testclient import TestClient

from gca.jobs.lifecycle import transition_job
from gca.jobs.models import JobStatus, RepositorySpec, RunSpec
from gca.session import SessionStore, WorkflowState
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


def test_get_run_includes_session_progress_fields(tmp_path: Path) -> None:
    state = ServiceState.build(_settings(tmp_path))
    job = state.store.create(
        RunSpec(
            task="Make progress",
            repository=RepositorySpec("https://example.test/owner/repo.git"),
        )
    )
    workspace = state.settings.workspace_root / job.id
    sessions = SessionStore(workspace / "sessions")
    session = sessions.create(job.run_spec.task)
    session.step_count = 7
    session.status = "active"
    session.workflow = WorkflowState(name="feature", phase="implementation")
    sessions.save(session)
    job.workspace_path = str(workspace / "repo")
    job.session_id = session.id
    state.store.save(job)
    client = TestClient(create_app(state=state))

    response = client.get(
        f"/runs/{job.id}",
        headers={"Authorization": "Bearer api-token-123456"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["step_count"] == 7
    assert payload["workflow"] == {"phase": "implementation"}
    assert payload["session_status"] == "active"


def test_run_can_be_cancelled_before_claim(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    headers = {"Authorization": "Bearer api-token-123456"}
    created = client.post("/runs", json=_run_payload(), headers=headers).json()

    response = client.post(f"/runs/{created['id']}/cancel", headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_running_job_can_be_requeued_and_exposes_lease(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    state = ServiceState.build(settings)
    client = TestClient(create_app(state=state))
    headers = {"Authorization": "Bearer api-token-123456"}
    created = client.post("/runs", json=_run_payload(), headers=headers).json()
    claimed = state.queue.claim(settings.worker_id, lease_seconds=1800)
    assert claimed is not None
    assert claimed.id == created["id"]

    status = client.get(f"/runs/{created['id']}", headers=headers)
    assert status.status_code == 200
    assert status.json()["lease_owner"] == settings.worker_id
    assert status.json()["lease_expires_at"] is not None

    response = client.post(f"/runs/{created['id']}/requeue", headers=headers)

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["lease_owner"] is None


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


def test_github_merged_pull_request_webhook_cleans_up_job(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    state = ServiceState.build(settings)
    job = state.store.create(
        RunSpec(
            task="Follow up",
            repository=RepositorySpec(
                url="https://github.com/owner/repo.git",
                ref="gca/head",
            ),
            labels={"pr_id": "46", "head_ref": "gca/head", "provider": "github"},
        )
    )
    state.queue.enqueue(job.id)
    claimed = state.queue.claim(settings.worker_id)
    assert claimed is not None
    transition_job(claimed, JobStatus.PAUSED, error="waiting")
    workspace = settings.workspace_root / claimed.id
    (workspace / "sessions").mkdir(parents=True)
    (workspace / "repo").mkdir(parents=True)
    (workspace / "sessions" / "s.json").write_text("{}", encoding="utf-8")
    claimed.workspace_path = str(workspace / "repo")
    claimed.publication = {
        "change_request_url": "https://github.com/owner/repo/pull/46",
    }
    state.store.save(claimed)

    client = TestClient(create_app(settings))
    body = json.dumps(
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
                "head": {"ref": "gca/head"},
            },
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
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-pr-merged",
            "X-Hub-Signature-256": signature,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "merged_pull_request_cleanup"
    assert claimed.id in payload["cancelled_job_ids"]
    assert claimed.id in payload["wiped_workspaces"]
    reloaded = ServiceState.build(settings).store.load(claimed.id)
    assert reloaded.status == JobStatus.CANCELLED
    assert reloaded.workspace_path is None
    assert not workspace.exists()


def test_github_pull_request_review_webhook_enqueues_run(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))
    body = json.dumps(
        {
            "action": "submitted",
            "review": {
                "state": "changes_requested",
                "body": "Please cover the GitHub /runs resume path.",
            },
            "repository": {
                "full_name": "owner/repo",
                "clone_url": "https://github.com/owner/repo.git",
                "default_branch": "main",
            },
            "pull_request": {
                "number": 43,
                "title": "Budget pause follow-up",
                "head": {"ref": "gca/review-head"},
                "base": {"ref": "main"},
            },
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
            "X-GitHub-Event": "pull_request_review",
            "X-GitHub-Delivery": "delivery-pr-review",
            "X-Hub-Signature-256": signature,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 202
    job = ServiceState.build(settings).store.load(response.json()["id"])
    assert job.run_spec.repository.ref == "gca/review-head"
    assert job.run_spec.publication is not None
    assert job.run_spec.publication.base_ref == "main"
    assert job.run_spec.labels["pr_id"] == "43"
    assert job.run_spec.labels["source"] == "pull_request_review.changes_requested"


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
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["worker"]["queued_count"] == 0


def test_ready_fails_when_queued_jobs_have_no_recent_worker_claim(tmp_path: Path) -> None:
    settings = ServiceSettings(
        data_dir=tmp_path / "service-ready-fail",
        api_token="api-token-123456",
        allowed_repository_hosts=frozenset({"example.test", "github.com"}),
        ready_worker_claim_timeout_seconds=1,
    )
    state = ServiceState.build(settings)
    job = state.store.create(
        RunSpec(
            task="Queued work",
            repository=RepositorySpec("https://example.test/owner/repo.git"),
        )
    )
    state.queue.enqueue(job.id)
    client = TestClient(create_app(state=state))

    response = client.get("/ready")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert payload["worker"]["queued_count"] == 1
    assert payload["worker"]["seconds_since_last_claim"] is None


def test_ready_reports_recent_worker_claim(tmp_path: Path) -> None:
    settings = ServiceSettings(
        data_dir=tmp_path / "service-ready-worker",
        api_token="api-token-123456",
        allowed_repository_hosts=frozenset({"example.test", "github.com"}),
        ready_worker_claim_timeout_seconds=60,
    )
    state = ServiceState.build(settings)
    job = state.store.create(
        RunSpec(
            task="Queued work",
            repository=RepositorySpec("https://example.test/owner/repo.git"),
        )
    )
    state.queue.enqueue(job.id)
    state.store.record_worker_heartbeat(settings.worker_id, claimed=True)
    client = TestClient(create_app(state=state))

    response = client.get("/ready")

    assert response.status_code == 200
    payload = response.json()
    assert payload["worker"]["worker_count"] == 1
    assert payload["worker"]["queued_count"] == 1
    assert payload["worker"]["seconds_since_last_claim"] is not None


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


def test_create_run_keeps_publication_when_publish_mode_branch(tmp_path: Path) -> None:
    settings = ServiceSettings(
        data_dir=tmp_path / "service-publish-branch",
        api_token="api-token-123456",
        allowed_repository_hosts=frozenset({"example.test", "github.com"}),
        github_token="github-token-for-tests",
        publish_mode="branch",
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
    assert job.run_spec.publication is not None
    assert job.run_spec.publication.provider == "github"


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
