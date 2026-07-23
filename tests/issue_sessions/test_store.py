"""Unit tests for durable issue-session storage."""

from __future__ import annotations

from pathlib import Path

import pytest

from gca.issue_sessions.models import (
    GenerationStatus,
    InboundEvent,
    IssueGeneration,
    IssueSession,
    OutboundAction,
    Turn,
    TurnStatus,
)
from gca.issue_sessions.store import (
    DuplicateDeliveryError,
    IssueSessionConcurrencyError,
    IssueSessionStore,
)


def test_unit_of_work_creates_session_generation_turn_and_job(tmp_path: Path) -> None:
    store = IssueSessionStore(tmp_path / "jobs.sqlite3")
    with store.unit_of_work() as uow:
        session = uow.upsert_session(
            IssueSession(
                gitlab_instance="https://gitlab.com",
                project_id=42,
                issue_iid=7,
                project_path="group/project",
                issue_title="Fix null metadata",
                repository_url="https://gitlab.com/group/project.git",
            )
        )
        generation = uow.insert_generation(
            IssueGeneration(
                issue_session_id=session.id,
                target_branch="develop",
                branch_name=f"gca/issues/7/{session.id[:8]}",
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
                max_steps=20,
            )
        )
        job = uow.create_turn_job(
            turn=turn,
            session=session,
            generation=generation,
            task="Fix the bug",
        )
        uow.save_turn(turn)
        uow.append_event(
            issue_session_id=session.id,
            generation_id=generation.id,
            turn_id=turn.id,
            kind="lifecycle",
            payload={"status": "queued"},
        )
        event = uow.insert_inbound_event(
            InboundEvent(
                provider="gitlab",
                gitlab_instance="https://gitlab.com",
                project_id=42,
                delivery_id="delivery-1",
                event_uuid="event-1",
                event_type="Issue Hook",
                action="open",
                object_key="issue:7:open",
                issue_session_id=session.id,
                generation_id=generation.id,
                authorized=True,
                payload={"title": "Fix null metadata"},
            )
        )
        uow.insert_outbound_action(
            OutboundAction(
                issue_session_id=session.id,
                generation_id=generation.id,
                turn_id=turn.id,
                kind="issue_note",
                effect_key=f"note:{session.id}:ack",
                payload={"template": "ack"},
            )
        )

    loaded = store.get_session(session.id)
    assert loaded.issue_iid == 7
    assert loaded.active_generation_id == generation.id
    assert store.get_generation(generation.id).target_branch == "develop"
    assert store.get_turn(turn.id).job_id == job.id
    assert store.list_events(session.id)[0].kind == "lifecycle"
    assert event.delivery_id == "delivery-1"
    assert store.list_pending_outbound()[0].kind == "issue_note"


def test_duplicate_delivery_is_rejected(tmp_path: Path) -> None:
    store = IssueSessionStore(tmp_path / "jobs.sqlite3")
    event = InboundEvent(
        provider="gitlab",
        gitlab_instance="https://gitlab.com",
        project_id=1,
        delivery_id="same",
        event_uuid="uuid",
        event_type="Note Hook",
        action="create",
        object_key="note:9:create",
    )
    with store.unit_of_work() as uow:
        uow.insert_inbound_event(event)
    with pytest.raises(DuplicateDeliveryError):
        with store.unit_of_work() as uow:
            uow.insert_inbound_event(
                InboundEvent(
                    provider="gitlab",
                    gitlab_instance="https://gitlab.com",
                    project_id=1,
                    delivery_id="same",
                    event_uuid="uuid-2",
                    event_type="Note Hook",
                    action="create",
                    object_key="note:10:create",
                )
            )


def test_one_active_turn_per_generation(tmp_path: Path) -> None:
    store = IssueSessionStore(tmp_path / "jobs.sqlite3")
    with store.unit_of_work() as uow:
        session = uow.upsert_session(
            IssueSession(
                gitlab_instance="https://gitlab.com",
                project_id=1,
                issue_iid=1,
                project_path="g/p",
                issue_title="t",
                repository_url="https://gitlab.com/g/p.git",
            )
        )
        generation = uow.insert_generation(IssueGeneration(issue_session_id=session.id))
        uow.insert_turn(
            Turn(
                issue_session_id=session.id,
                generation_id=generation.id,
                kind="code",
                status=TurnStatus.QUEUED,
            )
        )
        with pytest.raises(IssueSessionConcurrencyError):
            uow.insert_turn(
                Turn(
                    issue_session_id=session.id,
                    generation_id=generation.id,
                    kind="code",
                    status=TurnStatus.QUEUED,
                )
            )
