"""Hardening coverage for fencing, lifecycle, and coalesced comments."""

from __future__ import annotations

from pathlib import Path

from gca.integrations.gitlab_events import NormalizedGitLabEvent
from gca.integrations.webhook_registration import WebhookRegistration
from gca.issue_sessions.ingestion import IssueSessionIngestor, StaticMembershipChecker
from gca.issue_sessions.models import (
    GenerationStatus,
    IssueGeneration,
    IssueSession,
    OutboundAction,
    Turn,
    TurnStatus,
)
from gca.issue_sessions.outbox import OutboxProcessor, RecordingGitLabApiClient
from gca.issue_sessions.store import IssueSessionStore


def _registration() -> WebhookRegistration:
    return WebhookRegistration.from_mapping(
        {
            "id": "reg1prod",
            "gitlab_instance": "https://gitlab.com",
            "project_id": 42,
            "project_path": "group/project",
            "hook_uuid": "hook-1",
            "signing_secret": "super-secret-signing-token",
            "repository_url": "https://gitlab.com/group/project.git",
            "actor_allowlist": [7],
        }
    )


def _start_event(*, delivery_id: str = "d1") -> NormalizedGitLabEvent:
    return NormalizedGitLabEvent(
        delivery_id=delivery_id,
        event_uuid=delivery_id,
        event_type="Issue Hook",
        action="open",
        object_key=f"issue:3:open:{delivery_id}",
        gitlab_instance="https://gitlab.com",
        project_id=42,
        project_path="group/project",
        repository_url="https://gitlab.com/group/project.git",
        issue_iid=3,
        issue_title="Alert",
        issue_description="body",
        actor_id=7,
        actor_username="dev",
        label_added=True,
        labels=frozenset({"gca-run"}),
        target_branch="main",
        relevant=True,
    )


def test_issue_close_cancels_unpublished_generation(tmp_path: Path) -> None:
    store = IssueSessionStore(tmp_path / "db.sqlite3")
    ingestor = IssueSessionIngestor(
        store,
        membership=StaticMembershipChecker({(42, 7): 40}),
    )
    reg = _registration()
    started = ingestor.ingest(_start_event(), registration=reg)
    assert started.status == "accepted"
    close = NormalizedGitLabEvent(
        delivery_id="close-1",
        event_uuid="close-1",
        event_type="Issue Hook",
        action="close",
        object_key="issue:3:close",
        gitlab_instance="https://gitlab.com",
        project_id=42,
        project_path="group/project",
        repository_url="https://gitlab.com/group/project.git",
        issue_iid=3,
        actor_id=7,
        actor_username="dev",
        relevant=True,
    )
    result = ingestor.ingest(close, registration=reg)
    assert result.status == "accepted"
    session = store.get_session(started.issue_session_id or "")
    assert session.status == GenerationStatus.CANCELLED
    generation = store.get_generation(started.generation_id or "")
    assert generation.cancel_requested is True


def test_agent_run_is_idempotent_while_active(tmp_path: Path) -> None:
    store = IssueSessionStore(tmp_path / "db.sqlite3")
    ingestor = IssueSessionIngestor(
        store,
        membership=StaticMembershipChecker({(42, 7): 40}),
    )
    reg = _registration()
    started = ingestor.ingest(_start_event(), registration=reg)
    assert started.status == "accepted"
    run = NormalizedGitLabEvent(
        delivery_id="run-2",
        event_uuid="run-2",
        event_type="Note Hook",
        action="create",
        object_key="note:9:create",
        gitlab_instance="https://gitlab.com",
        project_id=42,
        project_path="group/project",
        repository_url="https://gitlab.com/group/project.git",
        issue_iid=3,
        actor_id=7,
        actor_username="dev",
        note_body="/agent run",
        command="/agent run",
        relevant=True,
    )
    again = ingestor.ingest(run, registration=reg)
    assert again.status == "accepted"
    assert again.job_id is None
    pending = store.list_pending_outbound()
    assert any(action.kind == "issue_note" for action in pending)


def test_outbox_skips_cancelled_generation(tmp_path: Path) -> None:
    store = IssueSessionStore(tmp_path / "db.sqlite3")
    with store.unit_of_work() as uow:
        session = uow.upsert_session(
            IssueSession(
                gitlab_instance="https://gitlab.com",
                project_id=42,
                issue_iid=1,
                project_path="g/p",
                issue_title="t",
                repository_url="https://gitlab.com/g/p.git",
            )
        )
        generation = uow.insert_generation(
            IssueGeneration(
                issue_session_id=session.id,
                status=GenerationStatus.CANCELLED,
                cancel_requested=True,
            )
        )
        uow.insert_outbound_action(
            OutboundAction(
                issue_session_id=session.id,
                generation_id=generation.id,
                kind="issue_note",
                effect_key="note:x",
                payload={"template": "ack", "issue_iid": 1},
            )
        )
    api = RecordingGitLabApiClient()
    OutboxProcessor(store, api).process_pending()
    assert not api.notes


def test_create_turn_job_prefers_bot_branch(tmp_path: Path) -> None:
    store = IssueSessionStore(tmp_path / "db.sqlite3")
    with store.unit_of_work() as uow:
        session = uow.upsert_session(
            IssueSession(
                gitlab_instance="https://gitlab.com",
                project_id=42,
                issue_iid=1,
                project_path="g/p",
                issue_title="t",
                repository_url="https://gitlab.com/g/p.git",
            )
        )
        generation = uow.insert_generation(
            IssueGeneration(
                issue_session_id=session.id,
                branch_name="gca/issues/1/abc",
                target_branch="main",
            )
        )
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
            task="do work",
        )
    assert job.run_spec.repository.ref == "gca/issues/1/abc"
