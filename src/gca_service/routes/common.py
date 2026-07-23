"""Shared route validation and response helpers."""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from gca.jobs.models import Job
from gca_service.auth import is_authorized
from gca_service.state import ServiceState


class RequestBodyTooLarge(ValueError):
    """Raised when an HTTP body exceeds the configured service limit."""


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

    body = await read_body(request, max_bytes=max_bytes)
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("request JSON must be an object")
    return value


async def read_body(request: Request, *, max_bytes: int) -> bytes:
    """Stream a request body while enforcing a hard byte limit."""

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length header") from exc
        if declared_length < 0:
            raise ValueError("invalid Content-Length header")
        if declared_length > max_bytes:
            raise RequestBodyTooLarge("request body is too large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            raise RequestBodyTooLarge("request body is too large")
        chunks.append(chunk)
    return b"".join(chunks)


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
        "result_summary": job.result_summary,
        "last_error": job.last_error,
        "labels": job.run_spec.labels,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }
