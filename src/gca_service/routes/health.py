"""Service liveness and readiness routes."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from gca_service.routes.common import service_state


async def health(request: Request) -> JSONResponse:
    """Return process liveness."""

    return JSONResponse({"status": "ok"})


async def ready(request: Request) -> JSONResponse:
    """Check durable job-store availability."""

    state = service_state(request)
    try:
        state.store.list(limit=1)
    except Exception:
        return JSONResponse({"status": "not_ready"}, status_code=503)
    return JSONResponse({"status": "ready"})
