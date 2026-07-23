"""Authenticated generic run submission and status routes."""

from __future__ import annotations

from dataclasses import replace

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
    RequestBodyTooLarge,
    apply_default_max_steps,
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
        max_attempts = payload.pop("max_attempts", 3)
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise ValueError("max_attempts must be an integer")
        spec = apply_default_max_steps(RunSpec.from_dict(payload), state.settings)
        validate_repository_spec(
            spec.repository,
            allowed_hosts=state.settings.allowed_repository_hosts,
            allow_local=state.settings.allow_local_repositories,
        )
        can_publish, error = state.can_publish(spec.publication)
        if not can_publish:
            return JSONResponse({"error": error}, status_code=503)

        job = state.store.create(
            spec,
            idempotency_key=request.headers.get("idempotency-key"),
            max_attempts=max_attempts,
        )
        if job.status == JobStatus.QUEUED:
            job = state.queue.enqueue(job.id)
    except RequestBodyTooLarge as exc:
        return JSONResponse({"error": str(exc)}, status_code=413)
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


async def resume_run(request: Request) -> JSONResponse:
    """Requeue a paused job with an optionally increased step budget."""

    unauthorized = require_auth(request)
    if unauthorized is not None:
        return unauthorized
    state = service_state(request)
    try:
        job = state.store.load(request.path_params["job_id"])
        if job.status != JobStatus.PAUSED:
            return JSONResponse(
                {"error": f"job cannot be resumed from {job.status.value}"},
                status_code=409,
            )
        payload = await read_json(request, max_bytes=state.settings.max_request_bytes)
        unknown = sorted(set(payload) - {"max_steps"})
        if unknown:
            raise ValueError(f"unknown resume keys: {', '.join(unknown)}")
        max_steps = payload.get("max_steps")
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps <= 0:
            raise ValueError("resume max_steps must be a positive integer")
        prior = job.run_spec.max_steps or 0
        if max_steps <= prior:
            raise ValueError("resume max_steps must exceed the prior job step budget")
        job.run_spec = replace(job.run_spec, max_steps=max_steps)
        state.store.save(job)
        job = state.queue.enqueue(job.id)
    except RequestBodyTooLarge as exc:
        return JSONResponse({"error": str(exc)}, status_code=413)
    except JobNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except (ValueError, JobTransitionError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(job_payload(job), status_code=202)
