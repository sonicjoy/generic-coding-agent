"""Authenticated issue-session operator APIs."""

from __future__ import annotations

import uuid
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from gca.integrations.gitlab_events import NormalizedGitLabEvent
from gca.integrations.webhooks import issue_task
from gca.issue_sessions.models import (
    GenerationStatus,
    IssueGeneration,
    IssueSession,
    Turn,
    TurnStatus,
)
from gca.issue_sessions.store import IssueSessionNotFoundError
from gca_service.routes.common import read_json, require_auth, service_state


async def list_issue_sessions(request: Request) -> Response:
    """List durable issue sessions."""

    unauthorized = require_auth(request)
    if unauthorized is not None:
        return unauthorized
    state = service_state(request)
    project_id = request.query_params.get("project_id")
    status = request.query_params.get("status")
    after_updated_at = request.query_params.get("after_updated_at")
    after_id = request.query_params.get("after_id")
    try:
        limit = int(request.query_params.get("limit", "50"))
        parsed_project = int(project_id) if project_id is not None else None
        parsed_status = GenerationStatus(status) if status else None
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    sessions = state.issue_store.list_sessions(
        project_id=parsed_project,
        status=parsed_status,
        limit=limit,
        after_updated_at=after_updated_at,
        after_id=after_id,
    )
    _audit(state, "list_issue_sessions", {"count": len(sessions)})
    return JSONResponse({"items": [_session_payload(item) for item in sessions]})


async def get_issue_session(request: Request) -> Response:
    """Return one issue session summary."""

    unauthorized = require_auth(request)
    if unauthorized is not None:
        return unauthorized
    state = service_state(request)
    session_id = str(request.path_params["session_id"])
    try:
        session = state.issue_store.get_session(session_id)
    except IssueSessionNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    generation = None
    scm = None
    if session.active_generation_id:
        generation = state.issue_store.get_generation(session.active_generation_id)
        scm = state.issue_store.get_scm_link(session.active_generation_id)
    _audit(state, "get_issue_session", {"issue_session_id": session.id})
    return JSONResponse(
        {
            **_session_payload(session),
            "generation": generation.to_dict() if generation else None,
            "scm": scm.to_dict() if scm else None,
        }
    )


async def list_issue_session_events(request: Request) -> Response:
    """Return paginated redacted events for one issue session."""

    unauthorized = require_auth(request)
    if unauthorized is not None:
        return unauthorized
    state = service_state(request)
    session_id = str(request.path_params["session_id"])
    try:
        after_seq = int(request.query_params.get("after_seq", "0"))
        limit = int(request.query_params.get("limit", "100"))
        state.issue_store.get_session(session_id)
    except (ValueError, IssueSessionNotFoundError) as exc:
        status = 404 if isinstance(exc, IssueSessionNotFoundError) else 400
        return JSONResponse({"error": str(exc)}, status_code=status)
    events = state.issue_store.list_events(session_id, after_seq=after_seq, limit=limit)
    _audit(
        state,
        "list_issue_session_events",
        {"issue_session_id": session_id, "count": len(events)},
    )
    return JSONResponse({"items": [event.to_dict() for event in events]})


async def get_issue_session_transcript(request: Request) -> Response:
    """Export a redacted transcript suitable for offline evaluation."""

    unauthorized = require_auth(request)
    if unauthorized is not None:
        return unauthorized
    state = service_state(request)
    session_id = str(request.path_params["session_id"])
    try:
        session = state.issue_store.get_session(session_id)
    except IssueSessionNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    events = state.issue_store.list_events(session_id, after_seq=0, limit=500)
    generation = None
    if session.active_generation_id:
        generation = state.issue_store.get_generation(session.active_generation_id)
    _audit(state, "get_issue_session_transcript", {"issue_session_id": session_id})
    return JSONResponse(
        {
            "export_schema_version": 1,
            "issue_session_id": session.id,
            "status": session.status.value,
            "project_id": session.project_id,
            "issue_iid": session.issue_iid,
            "generation_id": generation.id if generation else None,
            "target_branch": generation.target_branch if generation else None,
            "policy_fingerprint": generation.policy_fingerprint if generation else None,
            "events": [event.to_dict() for event in events],
        }
    )


async def cancel_issue_session(request: Request) -> Response:
    """Request cancellation of the active generation."""

    unauthorized = require_auth(request)
    if unauthorized is not None:
        return unauthorized
    state = service_state(request)
    session_id = str(request.path_params["session_id"])
    try:
        with state.issue_store.unit_of_work() as uow:
            session = uow.get_session(session_id)
            if session.status == GenerationStatus.COMPLETED:
                return JSONResponse(_session_payload(session))
            if not session.active_generation_id:
                raise IssueSessionNotFoundError("issue session has no active generation")
            generation = uow.get_generation(session.active_generation_id)
            generation.cancel_requested = True
            generation.status = GenerationStatus.CANCELLED
            uow.save_generation(generation)
            session.status = GenerationStatus.CANCELLED
            uow.save_session(session)
            uow.append_event(
                issue_session_id=session.id,
                generation_id=generation.id,
                kind="lifecycle",
                payload={"status": "cancelled", "source": "api"},
            )
    except IssueSessionNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    _audit(state, "cancel_issue_session", {"issue_session_id": session_id})
    return JSONResponse(_session_payload(session))


async def retry_issue_session(request: Request) -> Response:
    """Retry or reopen work for one issue session according to wait-reason rules."""

    unauthorized = require_auth(request)
    if unauthorized is not None:
        return unauthorized
    state = service_state(request)
    session_id = str(request.path_params["session_id"])
    try:
        body = await read_json(request, max_bytes=state.settings.max_request_bytes)
    except Exception:
        body = {}
    max_steps = body.get("max_steps") if isinstance(body, dict) else None
    try:
        with state.issue_store.unit_of_work() as uow:
            session = uow.get_session(session_id)
            if session.status == GenerationStatus.COMPLETED:
                return JSONResponse(
                    {"error": "completed sessions cannot be retried"},
                    status_code=400,
                )
            if not session.active_generation_id:
                raise IssueSessionNotFoundError("issue session has no active generation")
            generation = uow.get_generation(session.active_generation_id)
            if uow.active_turn(generation.id) is not None:
                return JSONResponse(
                    {"error": "generation already has an active turn"},
                    status_code=409,
                )
            turn = uow.insert_turn(
                Turn(
                    issue_session_id=session.id,
                    generation_id=generation.id,
                    kind="retry",
                    status=TurnStatus.QUEUED,
                    max_steps=int(max_steps) if isinstance(max_steps, int) else 25,
                    lease_epoch=generation.lease_epoch,
                )
            )
            job = uow.create_turn_job(
                turn=turn,
                session=session,
                generation=generation,
                task=(
                    "Operator retry requested for this GitLab issue session. "
                    f"Previous summary: {generation.summary or 'none'}"
                ),
                max_steps=turn.max_steps,
            )
            turn = uow.save_turn(turn)
            generation.status = GenerationStatus.QUEUED
            generation.wait_reason = None
            generation.cancel_requested = False
            uow.save_generation(generation)
            session.status = GenerationStatus.QUEUED
            uow.save_session(session)
            uow.append_event(
                issue_session_id=session.id,
                generation_id=generation.id,
                turn_id=turn.id,
                kind="lifecycle",
                payload={"status": "queued", "source": "api_retry", "job_id": job.id},
            )
    except IssueSessionNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    _audit(state, "retry_issue_session", {"issue_session_id": session_id})
    return JSONResponse({**_session_payload(session), "turn_id": turn.id, "job_id": job.id})


async def create_issue_session(request: Request) -> Response:
    """Operator-started GitLab issue session."""

    unauthorized = require_auth(request)
    if unauthorized is not None:
        return unauthorized
    state = service_state(request)
    try:
        body = await read_json(request, max_bytes=state.settings.max_request_bytes)
        registration_id = str(body["registration_id"])
        issue_iid = int(body["issue_iid"])
        issue_title = str(body.get("issue_title", f"Issue {issue_iid}"))
        issue_description = str(body.get("issue_description", ""))
    except Exception as exc:
        return JSONResponse({"error": f"invalid request: {exc}"}, status_code=400)
    registration = state.gitlab_registrations.get(registration_id)
    if registration is None:
        return JSONResponse({"error": "unknown registration_id"}, status_code=404)
    event = NormalizedGitLabEvent(
        delivery_id=f"api:{registration_id}:{issue_iid}:{issue_title}",
        event_uuid=f"api-{registration_id}-{issue_iid}",
        event_type="Issue Hook",
        action="open",
        object_key=f"api:issue:{issue_iid}:open",
        gitlab_instance=registration.gitlab_instance,
        project_id=registration.project_id,
        project_path=registration.project_path,
        repository_url=registration.repository_url
        or f"{registration.gitlab_instance}/{registration.project_path}.git",
        issue_iid=issue_iid,
        issue_title=issue_title,
        issue_description=issue_description,
        actor_id=0,
        actor_username="operator",
        label_added=True,
        labels=frozenset({registration.trigger_label}),
        target_branch=str(body.get("target_branch", "main")),
        relevant=True,
    )
    # Force authorization for operator API starts.
    result = state.issue_ingestor.ingest(event, registration=registration)
    if result.status == "ignored":
        # Operator API bypasses membership by using allowlist-like start path via label_added.
        with state.issue_store.unit_of_work() as uow:
            session = uow.upsert_session(
                IssueSession(
                    gitlab_instance=registration.gitlab_instance,
                    project_id=registration.project_id,
                    issue_iid=issue_iid,
                    project_path=registration.project_path,
                    issue_title=issue_title,
                    repository_url=event.repository_url,
                    registration_id=registration.id,
                    trigger_label=registration.trigger_label,
                )
            )
            generation = uow.insert_generation(
                IssueGeneration(
                    issue_session_id=session.id,
                    target_branch=event.target_branch or "main",
                    branch_name=f"gca/issues/{issue_iid}/{uuid.uuid4().hex[:12]}",
                )
            )
            session.active_generation_id = generation.id
            session.status = GenerationStatus.QUEUED
            uow.save_session(session)
            turn = uow.insert_turn(
                Turn(
                    issue_session_id=session.id,
                    generation_id=generation.id,
                    kind="code",
                    status=TurnStatus.QUEUED,
                )
            )
            job = uow.create_turn_job(
                turn=turn,
                session=session,
                generation=generation,
                task=issue_task(issue_title, issue_description),
            )
            turn = uow.save_turn(turn)
            uow.append_event(
                issue_session_id=session.id,
                generation_id=generation.id,
                turn_id=turn.id,
                kind="lifecycle",
                payload={"status": "queued", "source": "api"},
            )
            result_payload = {
                "status": "accepted",
                "issue_session_id": session.id,
                "generation_id": generation.id,
                "turn_id": turn.id,
                "job_id": job.id,
            }
        _audit(state, "create_issue_session", result_payload)
        return JSONResponse(result_payload, status_code=202)
    payload = {
        "status": result.status,
        "issue_session_id": result.issue_session_id,
        "generation_id": result.generation_id,
        "turn_id": result.turn_id,
        "job_id": result.job_id,
    }
    _audit(state, "create_issue_session", payload)
    return JSONResponse(payload, status_code=202)


def _session_payload(session: IssueSession) -> dict[str, Any]:
    return {
        "id": session.id,
        "status": session.status.value,
        "gitlab_instance": session.gitlab_instance,
        "project_id": session.project_id,
        "project_path": session.project_path,
        "issue_iid": session.issue_iid,
        "issue_title": session.issue_title,
        "active_generation_id": session.active_generation_id,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


def _audit(state: Any, action: str, payload: dict[str, Any]) -> None:
    # Best-effort audit into the first available session event stream is intentionally
    # avoided here; API audits are appended only when an issue session id is known.
    if "issue_session_id" in payload and payload["issue_session_id"]:
        try:
            with state.issue_store.unit_of_work() as uow:
                uow.append_event(
                    issue_session_id=str(payload["issue_session_id"]),
                    kind="audit",
                    payload={"action": action, **payload},
                )
        except Exception:
            return
