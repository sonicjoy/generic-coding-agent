"""Shared route validation and response helpers."""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from gca.jobs.models import Job
from gca_service.auth import is_authorized
from gca_service.state import ServiceState


def service_state(request: Request) -> ServiceState:
    """Return application service state."""

    return request.app.state.gca


def require_auth(request: Request) -> JSONResponse | None:
    """Return a 401 response when bearer authentication fails."""

    state = service_state(request)
    if is_authorized(request, state.settings.api_token):
        return None
    return JSONResponse({"error": "unauthorized"}, status_code=401)


async def read_json(request: Request, *, max_bytes: int) -> dict[str, Any]:
    """Read one bounded JSON object."""

    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > max_bytes:
        raise ValueError("request body is too large")
    body = await request.body()
    if len(body) > max_bytes:
        raise ValueError("request body is too large")
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("request JSON must be an object")
    return value


def job_payload(job: Job) -> dict[str, Any]:
    """Return the stable public representation of a job."""

    return {
        "id": job.id,
        "status": job.status.value,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "session_id": job.session_id,
        "workspace_path": job.workspace_path,
        "publication": job.publication,
        "last_error": job.last_error,
        "labels": job.run_spec.labels,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }
