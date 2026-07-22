"""Durable job-store protocol and transaction-safe SQLite implementation."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Protocol

from gca.jobs.models import Job, JobStatus, RunSpec, utc_now
from gca.routing import WORKFLOWS


class JobStoreError(RuntimeError):
    """Base error for durable job storage."""


class JobNotFoundError(JobStoreError):
    """Raised when a job ID does not exist."""


class JobConcurrencyError(JobStoreError):
    """Raised when optimistic concurrency detects a stale job update."""


class IdempotencyConflictError(JobStoreError):
    """Raised when an idempotency key is reused for a different request."""


class JobStore(Protocol):
    """Persistence contract consumed by workers and service routes."""

    def create(
        self,
        spec: RunSpec,
        *,
        idempotency_key: str | None = None,
        max_attempts: int = 3,
    ) -> Job: ...

    def save(self, job: Job) -> None: ...

    def load(self, job_id: str) -> Job: ...

    def find_by_idempotency(self, key: str) -> Job | None: ...

    def list(self, *, status: JobStatus | None = None, limit: int = 50) -> list[Job]: ...


class SqliteJobStore:
    """SQLite job store suitable for one-node and local deployments."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def create(
        self,
        spec: RunSpec,
        *,
        idempotency_key: str | None = None,
        max_attempts: int = 3,
    ) -> Job:
        """Create a queued job, returning the prior job on an exact replay."""

        _validate_run_spec(spec)
        if not 1 <= max_attempts <= 20:
            raise ValueError("max_attempts must be from 1 to 20")
        if idempotency_key is not None and not idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        if idempotency_key is not None:
            existing = self.find_by_idempotency(idempotency_key)
            if existing is not None:
                if existing.run_spec != spec:
                    raise IdempotencyConflictError(
                        "idempotency key is already associated with a different run"
                    )
                return existing

        job = Job(
            run_spec=spec,
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
        )
        payload = json.dumps(job.to_dict(), sort_keys=True)
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO jobs (
                        id, idempotency_key, repository_url, status, data, version, not_before,
                        lease_owner, lease_expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job.id,
                        job.idempotency_key,
                        job.run_spec.repository.url,
                        job.status.value,
                        payload,
                        job.version,
                        job.not_before,
                        job.lease_owner,
                        job.lease_expires_at,
                        job.created_at,
                        job.updated_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if idempotency_key is not None:
                existing = self.find_by_idempotency(idempotency_key)
                if existing is not None and existing.run_spec == spec:
                    return existing
            raise JobStoreError(f"could not create job: {exc}") from exc
        return job

    def save(self, job: Job) -> None:
        """Persist a job with optimistic version checking."""

        expected = job.version
        next_version = expected + 1
        job.updated_at = utc_now()
        payload = job.to_dict()
        payload["version"] = next_version
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?, data = ?, version = ?, not_before = ?,
                    lease_owner = ?, lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    job.status.value,
                    json.dumps(payload, sort_keys=True),
                    next_version,
                    job.not_before,
                    job.lease_owner,
                    job.lease_expires_at,
                    job.updated_at,
                    job.id,
                    expected,
                ),
            )
            if cursor.rowcount != 1:
                raise JobConcurrencyError(f"stale or missing job update: {job.id}")
        job.version = next_version

    def load(self, job_id: str) -> Job:
        """Load one job by ID."""

        with self._connect() as connection:
            row = connection.execute("SELECT data FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFoundError(f"no such job: {job_id}")
        return Job.from_dict(json.loads(str(row["data"])))

    def find_by_idempotency(self, key: str) -> Job | None:
        """Return the job associated with an idempotency key."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM jobs WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return Job.from_dict(json.loads(str(row["data"]))) if row is not None else None

    def list(self, *, status: JobStatus | None = None, limit: int = 50) -> list[Job]:
        """List newest jobs, optionally filtered by lifecycle status."""

        if not 1 <= limit <= 1000:
            raise ValueError("limit must be from 1 to 1000")
        query = "SELECT data FROM jobs"
        values: tuple[object, ...]
        if status is None:
            values = (limit,)
        else:
            query += " WHERE status = ?"
            values = (status.value, limit)
        query += " ORDER BY created_at DESC LIMIT ?"
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [Job.from_dict(json.loads(str(row["data"]))) for row in rows]

    def claim_next(self, worker_id: str, *, lease_seconds: int = 300) -> Job | None:
        """Atomically claim one due queued job."""

        if not worker_id.strip():
            raise ValueError("worker_id must not be empty")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT queued.data FROM jobs AS queued
                WHERE queued.status = ? AND queued.not_before <= ?
                  AND NOT EXISTS (
                    SELECT 1 FROM jobs AS active
                    WHERE active.repository_url = queued.repository_url
                      AND active.id != queued.id
                      AND active.status IN (?, ?)
                  )
                ORDER BY queued.created_at ASC
                LIMIT 1
                """,
                (
                    JobStatus.QUEUED.value,
                    now,
                    JobStatus.RUNNING.value,
                    JobStatus.PUBLISHING.value,
                ),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            job = Job.from_dict(json.loads(str(row["data"])))
            job.status = JobStatus.RUNNING
            job.attempt += 1
            job.lease_owner = worker_id
            job.lease_expires_at = now + lease_seconds
            job.updated_at = utc_now()
            next_version = job.version + 1
            payload = job.to_dict()
            payload["version"] = next_version
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?, data = ?, version = ?, lease_owner = ?,
                    lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND version = ? AND status = ?
                """,
                (
                    job.status.value,
                    json.dumps(payload, sort_keys=True),
                    next_version,
                    job.lease_owner,
                    job.lease_expires_at,
                    job.updated_at,
                    job.id,
                    job.version,
                    JobStatus.QUEUED.value,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise JobConcurrencyError(f"job was claimed concurrently: {job.id}")
            connection.commit()
            job.version = next_version
            return job

    def requeue_expired(self) -> int:
        """Requeue jobs whose worker lease expired."""

        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT data FROM jobs
                WHERE status = ? AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
                """,
                (JobStatus.RUNNING.value, now),
            ).fetchall()
            count = 0
            for row in rows:
                job = Job.from_dict(json.loads(str(row["data"])))
                job.status = JobStatus.QUEUED
                job.lease_owner = None
                job.lease_expires_at = None
                job.updated_at = utc_now()
                next_version = job.version + 1
                payload = job.to_dict()
                payload["version"] = next_version
                cursor = connection.execute(
                    """
                    UPDATE jobs
                    SET status = ?, data = ?, version = ?, lease_owner = NULL,
                        lease_expires_at = NULL, updated_at = ?
                    WHERE id = ? AND version = ?
                    """,
                    (
                        job.status.value,
                        json.dumps(payload, sort_keys=True),
                        next_version,
                        job.updated_at,
                        job.id,
                        job.version,
                    ),
                )
                count += cursor.rowcount
            connection.commit()
        return count

    def renew_lease(self, job_id: str, worker_id: str, *, lease_seconds: int) -> Job:
        """Extend a running job lease held by ``worker_id``."""

        job = self.load(job_id)
        if job.status != JobStatus.RUNNING or job.lease_owner != worker_id:
            raise JobConcurrencyError(f"worker does not hold running job lease: {job_id}")
        job.lease_expires_at = time.time() + lease_seconds
        self.save(job)
        return job

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE,
                    repository_url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    not_before REAL NOT NULL,
                    lease_owner TEXT,
                    lease_expires_at REAL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_status_due
                    ON jobs(status, not_before, created_at);
                CREATE INDEX IF NOT EXISTS jobs_lease
                    ON jobs(status, lease_expires_at);
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "repository_url" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN repository_url TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS jobs_repository_status
                    ON jobs(repository_url, status)
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection


def _validate_run_spec(spec: RunSpec) -> None:
    if not spec.task.strip():
        raise ValueError("run task must not be empty")
    if not spec.repository.url.strip():
        raise ValueError("repository URL must not be empty")
    if not spec.repository.ref.strip():
        raise ValueError("repository ref must not be empty")
    if not 1 <= spec.repository.shallow_depth <= 1000:
        raise ValueError("repository shallow_depth must be from 1 to 1000")
    if spec.workflow is not None and spec.workflow not in WORKFLOWS:
        raise ValueError(f"workflow must be one of: {', '.join(sorted(WORKFLOWS))}")
    if spec.max_steps is not None and not 1 <= spec.max_steps <= 1000:
        raise ValueError("max_steps must be from 1 to 1000")
