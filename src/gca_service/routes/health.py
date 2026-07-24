"""Service liveness and readiness routes."""

from __future__ import annotations

import time
from typing import Any

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
        queued_count = state.store.count_queued()
        worker = _worker_status(state.store.worker_liveness(), queued_count)
    except Exception:
        return JSONResponse({"status": "not_ready"}, status_code=503)
    payload: dict[str, Any] = {"status": "ready", "worker": worker}
    timeout = state.settings.ready_worker_claim_timeout_seconds
    claim_age = worker["seconds_since_last_claim"]
    if timeout > 0 and queued_count > 0 and (claim_age is None or claim_age > timeout):
        payload["status"] = "not_ready"
        return JSONResponse(payload, status_code=503)
    return JSONResponse(payload)


def _worker_status(liveness: dict[str, object], queued_count: int) -> dict[str, Any]:
    now = time.time()
    last_seen_at = liveness.get("last_seen_at")
    last_claimed_at = liveness.get("last_claimed_at")
    return {
        "worker_count": _as_int(liveness.get("worker_count"), default=0),
        "last_seen_at": last_seen_at,
        "seconds_since_last_seen": _age_seconds(last_seen_at, now),
        "last_claimed_at": last_claimed_at,
        "seconds_since_last_claim": _age_seconds(last_claimed_at, now),
        "queued_count": queued_count,
    }


def _as_int(value: object, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _age_seconds(timestamp: object, now: float) -> float | None:
    if timestamp is None:
        return None
    if isinstance(timestamp, bool):
        return None
    if isinstance(timestamp, (int, float)):
        return max(0.0, now - float(timestamp))
    if isinstance(timestamp, str):
        try:
            return max(0.0, now - float(timestamp))
        except ValueError:
            return None
    return None
