"""Verified provider webhook ingestion."""

from __future__ import annotations

import hashlib

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from gca.integrations.webhooks import (
    WebhookContext,
    WebhookPayloadError,
    WebhookVerificationError,
)
from gca.jobs.models import JobStatus
from gca.jobs.store import IdempotencyConflictError
from gca.workspace.prepare import WorkspaceError, validate_repository_spec
from gca_service.config import ServiceSettings
from gca_service.routes.common import (
    RequestBodyTooLarge,
    job_payload,
    read_body,
    service_state,
)


async def receive_webhook(request: Request) -> Response:
    """Verify, normalize, deduplicate, and enqueue one legacy SCM delivery."""

    state = service_state(request)
    provider = str(request.path_params["provider"])
    if provider == "gitlab" and len(state.gitlab_registrations) > 1:
        return JSONResponse(
            {
                "error": (
                    "ambiguous GitLab webhook configuration; use /webhooks/gitlab/{registration_id}"
                )
            },
            status_code=400,
        )
    if provider == "gitlab" and state.gitlab_registrations:
        registration_id = next(iter(state.gitlab_registrations))
        return await receive_gitlab_webhook(request, registration_id=registration_id)

    normalizer = state.normalizers.get(provider)
    if normalizer is None:
        return JSONResponse({"error": f"unsupported webhook provider: {provider}"}, status_code=404)
    secret, allowed_projects = _provider_policy(state.settings, provider)
    if not secret:
        return JSONResponse(
            {"error": f"{provider} webhooks are not configured"},
            status_code=503,
        )
    try:
        body = await read_body(request, max_bytes=state.settings.max_request_bytes)
    except RequestBodyTooLarge as exc:
        return JSONResponse({"error": str(exc)}, status_code=413)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    context = WebhookContext(
        provider=provider,
        headers=dict(request.headers),
        body=body,
    )
    try:
        normalizer.verify(context, secret)
        normalizer.delivery_id(context)
        spec = normalizer.normalize(context, allowed_projects=allowed_projects)
        if spec is None:
            return Response(status_code=204)
        validate_repository_spec(
            spec.repository,
            allowed_hosts=state.settings.allowed_repository_hosts,
            allow_local=False,
        )
        job = state.store.create(
            spec,
            idempotency_key=(f"webhook:{provider}:{hashlib.sha256(context.body).hexdigest()}"),
        )
        if job.status == JobStatus.QUEUED:
            job = state.queue.enqueue(job.id)
    except WebhookVerificationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=401)
    except (WebhookPayloadError, WorkspaceError, IdempotencyConflictError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(job_payload(job), status_code=202)


async def receive_gitlab_registered_webhook(request: Request) -> Response:
    """Ingest one registration-bound GitLab webhook delivery."""

    registration_id = str(request.path_params["registration_id"])
    return await receive_gitlab_webhook(request, registration_id=registration_id)


async def receive_gitlab_webhook(request: Request, *, registration_id: str) -> Response:
    """Verify and durably ingest a GitLab webhook for one registration."""

    state = service_state(request)
    registration = state.gitlab_registrations.get(registration_id)
    if registration is None:
        return JSONResponse({"error": "unknown webhook registration"}, status_code=404)
    try:
        body = await read_body(request, max_bytes=state.settings.max_request_bytes)
    except RequestBodyTooLarge:
        return JSONResponse({"error": "request body is too large"}, status_code=413)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    context = WebhookContext(provider="gitlab", headers=dict(request.headers), body=body)
    try:
        normalizer = state.gitlab_normalizer(registration_id)
        normalizer.verify(context)
        event = normalizer.normalize(context)
        result = state.issue_ingestor.ingest(event, registration=registration)
    except WebhookVerificationError as exc:
        message = str(exc)
        status = 403 if "does not match registration" in message else 401
        return JSONResponse({"error": message}, status_code=status)
    except WebhookPayloadError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception:
        return JSONResponse({"error": "durable ingestion failed"}, status_code=503)
    return JSONResponse(
        {
            "delivery_id": result.delivery_id,
            "status": result.status,
            "issue_session_id": result.issue_session_id,
        },
        status_code=202,
    )


def _provider_policy(settings: ServiceSettings, provider: str) -> tuple[str, frozenset[str]]:
    if provider == "github":
        return settings.github_webhook_secret, settings.allowed_github_projects
    if provider == "gitlab":
        return settings.gitlab_webhook_secret, settings.allowed_gitlab_projects
    return "", frozenset()
