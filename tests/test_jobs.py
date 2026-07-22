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
