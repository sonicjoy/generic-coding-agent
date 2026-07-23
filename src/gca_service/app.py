"""ASGI application factory for the optional hosted agent service."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.routing import Route

from gca_service.config import ServiceSettings
from gca_service.routes.health import health, ready
from gca_service.routes.runs import cancel_run, create_run, get_run, resume_run
from gca_service.routes.webhooks import receive_webhook
from gca_service.state import ServiceState


def create_app(
    settings: ServiceSettings | None = None,
    *,
    state: ServiceState | None = None,
) -> Starlette:
    """Create an ASGI app with explicitly injected or environment settings."""

    resolved_state = state or ServiceState.build(settings or ServiceSettings.from_environment())
    app = Starlette(
        debug=False,
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/ready", ready, methods=["GET"]),
            Route("/runs", create_run, methods=["POST"]),
            Route("/runs/{job_id}", get_run, methods=["GET"]),
            Route("/runs/{job_id}/cancel", cancel_run, methods=["POST"]),
            Route("/runs/{job_id}/resume", resume_run, methods=["POST"]),
            Route("/webhooks/{provider}", receive_webhook, methods=["POST"]),
        ],
    )
    app.state.gca = resolved_state
    return app
