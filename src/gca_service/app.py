"""ASGI application factory for the optional hosted agent service."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.routing import Route

from gca_service.config import ServiceSettings
from gca_service.routes.health import health, ready
from gca_service.routes.issue_sessions import (
    cancel_issue_session,
    create_issue_session,
    get_issue_session,
    get_issue_session_transcript,
    list_issue_session_events,
    list_issue_sessions,
    retry_issue_session,
)
from gca_service.routes.runs import cancel_run, create_run, get_run, requeue_run, resume_run
from gca_service.routes.webhooks import receive_gitlab_registered_webhook, receive_webhook
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
            Route("/runs/{job_id}/requeue", requeue_run, methods=["POST"]),
            Route("/runs/{job_id}/resume", resume_run, methods=["POST"]),
            Route("/issue-sessions", list_issue_sessions, methods=["GET"]),
            Route("/issue-sessions", create_issue_session, methods=["POST"]),
            Route("/issue-sessions/{session_id}", get_issue_session, methods=["GET"]),
            Route(
                "/issue-sessions/{session_id}/events",
                list_issue_session_events,
                methods=["GET"],
            ),
            Route(
                "/issue-sessions/{session_id}/transcript",
                get_issue_session_transcript,
                methods=["GET"],
            ),
            Route(
                "/issue-sessions/{session_id}/cancel",
                cancel_issue_session,
                methods=["POST"],
            ),
            Route(
                "/issue-sessions/{session_id}/retry",
                retry_issue_session,
                methods=["POST"],
            ),
            Route("/webhooks/{provider}", receive_webhook, methods=["POST"]),
            Route(
                "/webhooks/gitlab/{registration_id}",
                receive_gitlab_registered_webhook,
                methods=["POST"],
            ),
        ],
    )
    app.state.gca = resolved_state
    return app
