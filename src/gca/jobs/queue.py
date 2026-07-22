"""Job queue protocol backed by transactional SQLite leases."""

from __future__ import annotations

import time
from typing import Protocol

from gca.jobs.lifecycle import JobTransitionError, transition_job
from gca.jobs.models import Job, JobStatus
from gca.jobs.store import SqliteJobStore


class JobQueue(Protocol):
    """Queue contract used by API handlers and workers."""

    def enqueue(self, job_id: str, *, delay_seconds: int = 0) -> Job: ...

    def claim(self, worker_id: str, *, lease_seconds: int = 300) -> Job | None: ...

    def claim_job(
        self,
        job_id: str,
        worker_id: str,
        *,
        lease_seconds: int = 300,
    ) -> Job | None: ...

    def ack(self, job: Job) -> None: ...

    def nack(
        self,
        job: Job,
        *,
        requeue: bool,
        delay_seconds: int = 0,
        error: str = "",
    ) -> None: ...


class SqliteJobQueue:
    """Queue facade over :class:`SqliteJobStore` lifecycle and leases."""

    def __init__(self, store: SqliteJobStore) -> None:
        self.store = store

    def enqueue(self, job_id: str, *, delay_seconds: int = 0) -> Job:
        """Queue a new, paused, or failed job."""

        if delay_seconds < 0:
            raise ValueError("delay_seconds must not be negative")
        job = self.store.load(job_id)
        if job.status != JobStatus.QUEUED:
            transition_job(job, JobStatus.QUEUED)
        job.not_before = time.time() + delay_seconds
        self.store.save(job)
        return job

    def claim(self, worker_id: str, *, lease_seconds: int = 300) -> Job | None:
        """Claim one due job for a worker."""

        self.store.requeue_expired()
        return self.store.claim_next(worker_id, lease_seconds=lease_seconds)

    def claim_job(
        self,
        job_id: str,
        worker_id: str,
        *,
        lease_seconds: int = 300,
    ) -> Job | None:
        """Claim one known job without consuming another queued job."""

        self.store.requeue_expired()
        return self.store.claim(job_id, worker_id, lease_seconds=lease_seconds)

    def ack(self, job: Job) -> None:
        """Persist a terminal or paused result and clear its lease."""

        if job.status in {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PUBLISHING}:
            raise JobTransitionError(f"cannot acknowledge job in state {job.status.value}")
        job.lease_owner = None
        job.lease_expires_at = None
        self.store.save(job)

    def nack(
        self,
        job: Job,
        *,
        requeue: bool,
        delay_seconds: int = 0,
        error: str = "",
    ) -> None:
        """Retry or terminally fail a claimed job."""

        target = JobStatus.QUEUED if requeue else JobStatus.FAILED
        transition_job(job, target, error=error)
        job.not_before = time.time() + max(0, delay_seconds)
        self.store.save(job)
