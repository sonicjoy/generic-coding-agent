"""Authenticated generic run submission and status routes."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from gca.jobs.lifecycle import JobTransitionError, transition_job
from gca.jobs.models import JobStatus, RunSpec
from gca.jobs.store import (
    IdempotencyConflictError,
    JobNotFoundError,
)
from gca.workspace.prepare import WorkspaceError, validate_repository_spec
from gca_service.routes.common import (
    job_payload,
    read_json,
    require_auth,
    service_state,
)

_RUN_KEYS = {
    "task",
    "repository",
    "workflow",
    "max_steps",
    "publication",
    "labels",
    "max_attempts",
}


async def create_run(request: Request) -> JSONResponse:
    """Create and enqueue one authenticated generic repository run."""

    unauthorized = require_auth(request)
    if unauthorized is not None:
        return unauthorized
    state = service_state(request)
    try:
        payload = await read_json(request, max_bytes=state.settings.max_request_bytes)
        unknown = sorted(set(payload) - _RUN_KEYS)
        if unknown:
            raise ValueError(f"unknown run keys: {', '.join(unknown)}")
        max_attempts = int(payload.pop("max_attempts", 3))
        spec = RunSpec.from_dict(payload)
        validate_repository_spec(
            spec.repository,
            allowed_hosts=state.settings.allowed_repository_hosts,
            allow_local=state.settings.allow_local_repositories,
        )
        job = state.store.create(
            spec,
            idempotency_key=request.headers.get("idempotency-key"),
            max_attempts=max_attempts,
        )
        if job.status == JobStatus.QUEUED:
            job = state.queue.enqueue(job.id)
    except (ValueError, WorkspaceError, IdempotencyConflictError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(job_payload(job), status_code=202)


async def get_run(request: Request) -> JSONResponse:
    """Return durable status for one job."""

    unauthorized = require_auth(request)
    if unauthorized is not None:
        return unauthorized
    state = service_state(request)
    try:
        job = state.store.load(request.path_params["job_id"])
    except JobNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(job_payload(job))


async def cancel_run(request: Request) -> JSONResponse:
    """Cancel a queued or paused job."""

    unauthorized = require_auth(request)
    if unauthorized is not None:
        return unauthorized
    state = service_state(request)
    try:
        job = state.store.load(request.path_params["job_id"])
        if job.status not in {JobStatus.QUEUED, JobStatus.PAUSED}:
            return JSONResponse(
                {"error": f"job cannot be cancelled from {job.status.value}"},
                status_code=409,
            )
        transition_job(job, JobStatus.CANCELLED)
        state.store.save(job)
    except JobNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except JobTransitionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return JSONResponse(job_payload(job))
