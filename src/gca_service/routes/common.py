"""Shared route validation and response helpers."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from gca.jobs.models import Job, RunSpec
from gca.session import SessionStore
from gca_service.auth import is_authorized
from gca_service.config import ServiceSettings
from gca_service.state import ServiceState


class RequestBodyTooLarge(ValueError):
    """Raised when an HTTP body exceeds the configured service limit."""


_PUBLICATION_TOKEN_ENV = {"github": "GCA_GITHUB_TOKEN", "gitlab": "GCA_GITLAB_TOKEN"}


def enforce_publication_policy(spec: RunSpec, settings: ServiceSettings) -> RunSpec:
    """Reject or strip publication requests the service cannot fulfill.

    Webhooks always attach a publication target. Without a matching SCM token the
    worker would otherwise run the agent and fail only at publish time. Call this
    at enqueue so operators get a clear 400 naming the missing env var. Use
    ``GCA_PUBLISH_MODE=off`` for intentional dry runs, or ``branch`` to push
    without opening a change request.
    """

    if spec.publication is None:
        return spec
    if settings.publish_mode == "off":
        return replace(spec, publication=None)
    provider = spec.publication.provider
    token = {
        "github": settings.github_token,
        "gitlab": settings.gitlab_token,
    }.get(provider, "")
    if not token:
        env_var = _PUBLICATION_TOKEN_ENV.get(provider)
        hint = f"set {env_var}" if env_var else "configure an SCM token"
        raise ValueError(
            f"publication to '{provider}' requested but no SCM token is configured; "
            f"{hint} or set GCA_PUBLISH_MODE=off to run without publishing"
        )
    return spec


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


def apply_default_max_steps(spec: RunSpec, settings: ServiceSettings) -> RunSpec:
    """Fill ``max_steps`` from service settings when a run did not set one."""

    if spec.max_steps is not None or settings.default_max_steps is None:
        return spec
    return replace(spec, max_steps=settings.default_max_steps)


def job_payload(job: Job) -> dict[str, Any]:
    """Return the stable public representation of a job."""

    usage = dict(job.llm_usage or {})
    payload: dict[str, Any] = {
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
        "max_steps": job.run_spec.max_steps,
        "llm_usage": usage,
        "tokens_in": int(usage.get("prompt_tokens", 0) or 0),
        "tokens_out": int(usage.get("completion_tokens", 0) or 0),
        "cost_usd": float(usage.get("cost_usd", 0) or 0),
        "lease_owner": job.lease_owner,
        "lease_expires_at": job.lease_expires_at,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }
    payload.update(_session_progress(job))
    return payload


def _session_progress(job: Job) -> dict[str, Any]:
    if not job.session_id or not job.workspace_path:
        return {}
    try:
        session = SessionStore(Path(job.workspace_path).parent / "sessions").load(job.session_id)
    except (FileNotFoundError, OSError, ValueError):
        return {}
    workflow = {"phase": session.workflow.phase} if session.workflow is not None else None
    return {
        "step_count": session.step_count,
        "workflow": workflow,
        "session_status": session.status,
    }
