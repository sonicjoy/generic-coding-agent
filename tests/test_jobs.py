from __future__ import annotations

import time
from pathlib import Path

import pytest

from gca.jobs.lifecycle import JobTransitionError, transition_job
from gca.jobs.models import JobStatus, RepositorySpec, RunSpec
from gca.jobs.queue import SqliteJobQueue
from gca.jobs.store import (
    IdempotencyConflictError,
    JobConcurrencyError,
    SqliteJobStore,
)


def _spec(task: str = "Fix a typo") -> RunSpec:
    return RunSpec(
        task=task,
        repository=RepositorySpec("https://example.test/repo.git"),
        workflow="fast",
    )


def test_sqlite_store_is_idempotent_and_detects_conflicts(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.sqlite3")

    first = store.create(_spec(), idempotency_key="delivery-1")
    replay = store.create(_spec(), idempotency_key="delivery-1")

    assert replay.id == first.id
    with pytest.raises(IdempotencyConflictError):
        store.create(_spec("Different task"), idempotency_key="delivery-1")


def test_sqlite_queue_claims_once_and_checks_versions(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.sqlite3")
    queue = SqliteJobQueue(store)
    created = store.create(_spec())
    queue.enqueue(created.id)

    claimed = queue.claim("worker-a", lease_seconds=30)

    assert claimed is not None
    assert claimed.status == JobStatus.RUNNING
    assert claimed.attempt == 1
    assert queue.claim("worker-b") is None
    stale = store.load(claimed.id)
    claimed.last_error = "new"
    store.save(claimed)
    stale.last_error = "stale"
    with pytest.raises(JobConcurrencyError):
        store.save(stale)


def test_expired_lease_is_requeued(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.sqlite3")
    queue = SqliteJobQueue(store)
    created = store.create(_spec())
    queue.enqueue(created.id)
    claimed = queue.claim("worker-a", lease_seconds=1)
    assert claimed is not None
    claimed.lease_expires_at = time.time() - 1
    store.save(claimed)

    recovered = queue.claim("worker-b")

    assert recovered is not None
    assert recovered.id == claimed.id
    assert recovered.attempt == 2


def test_lifecycle_rejects_invalid_transition(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.sqlite3")
    job = store.create(_spec())

    with pytest.raises(JobTransitionError):
        transition_job(job, JobStatus.COMPLETED)


def test_queue_serializes_jobs_for_same_repository(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.sqlite3")
    queue = SqliteJobQueue(store)
    first = store.create(_spec("First task"))
    second = store.create(_spec("Second task"))
    queue.enqueue(first.id)
    queue.enqueue(second.id)

    active = queue.claim("worker-a")
    blocked = queue.claim("worker-b")

    assert active is not None
    assert blocked is None
    transition_job(active, JobStatus.COMPLETED)
    store.save(active)
    next_job = queue.claim("worker-b")
    assert next_job is not None
    assert next_job.id == second.id


def test_expired_final_attempt_becomes_failed(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.sqlite3")
    queue = SqliteJobQueue(store)
    created = store.create(_spec(), max_attempts=1)
    queue.enqueue(created.id)
    claimed = queue.claim("worker", lease_seconds=1)
    assert claimed is not None
    claimed.lease_expires_at = time.time() - 1
    store.save(claimed)

    assert queue.claim("other-worker") is None
    failed = store.load(created.id)
    assert failed.status == JobStatus.FAILED
    assert "lease expired" in failed.last_error


def test_specific_claim_does_not_consume_another_job(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.sqlite3")
    queue = SqliteJobQueue(store)
    first = store.create(_spec("First"))
    second = store.create(
        RunSpec(
            task="Second",
            repository=RepositorySpec("https://other.example/repo.git"),
            workflow="fast",
        )
    )
    queue.enqueue(first.id)
    queue.enqueue(second.id)

    claimed = queue.claim_job(second.id, "local-cli")

    assert claimed is not None and claimed.id == second.id
    assert store.load(first.id).status == JobStatus.QUEUED


def test_expired_publication_lease_can_retry_idempotently(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.sqlite3")
    queue = SqliteJobQueue(store)
    created = store.create(_spec(), max_attempts=2)
    queue.enqueue(created.id)
    claimed = queue.claim("worker", lease_seconds=30)
    assert claimed is not None
    transition_job(claimed, JobStatus.PUBLISHING)
    assert claimed.lease_owner == "worker"
    claimed.lease_expires_at = time.time() - 1
    store.save(claimed)

    retried = queue.claim("retry-worker")

    assert retried is not None
    assert retried.id == claimed.id
    assert retried.status == JobStatus.RUNNING
    assert retried.attempt == 2
