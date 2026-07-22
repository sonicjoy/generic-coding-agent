"""Validated job lifecycle transitions and retry policy."""

from __future__ import annotations

from gca.jobs.models import Job, JobStatus, utc_now

_TRANSITIONS = {
    JobStatus.QUEUED: {JobStatus.RUNNING, JobStatus.CANCELLED},
    JobStatus.RUNNING: {
        JobStatus.QUEUED,
        JobStatus.PAUSED,
        JobStatus.PUBLISHING,
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    },
    JobStatus.PAUSED: {JobStatus.QUEUED, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.PUBLISHING: {JobStatus.QUEUED, JobStatus.COMPLETED, JobStatus.FAILED},
    JobStatus.FAILED: {JobStatus.QUEUED},
    JobStatus.COMPLETED: set(),
    JobStatus.CANCELLED: set(),
}


class JobTransitionError(ValueError):
    """Raised when a job lifecycle transition is invalid."""


def transition_job(job: Job, status: JobStatus, *, error: str = "") -> Job:
    """Move ``job`` to a validated state and update lifecycle metadata."""

    if status == job.status:
        return job
    if status not in _TRANSITIONS[job.status]:
        raise JobTransitionError(f"invalid job transition: {job.status.value} -> {status.value}")
    job.status = status
    job.updated_at = utc_now()
    if error:
        job.last_error = error
    if status not in {JobStatus.RUNNING, JobStatus.PUBLISHING}:
        job.lease_owner = None
        job.lease_expires_at = None
    return job


def retry_delay_seconds(attempt: int) -> int:
    """Return bounded exponential retry delay for a one-based attempt."""

    schedule = (30, 120, 600)
    return schedule[min(max(attempt - 1, 0), len(schedule) - 1)]


def can_retry(job: Job) -> bool:
    """Return whether another execution attempt is available."""

    return job.attempt < job.max_attempts
