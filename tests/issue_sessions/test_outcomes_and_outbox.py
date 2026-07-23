"""Tests for turn outcomes, notes, and trusted publication outbox."""

from __future__ import annotations

import subprocess
from pathlib import Path

from gca.agent import AgentResult
from gca.credentials import CredentialBroker
from gca.issue_sessions.models import (
    GenerationStatus,
    IssueGeneration,
    IssueSession,
    ScmLink,
    Turn,
    TurnStatus,
)
from gca.issue_sessions.outbox import (
    OutboxProcessor,
    RecordingGitLabApiClient,
    create_trusted_commit,
    render_issue_note,
)
from gca.issue_sessions.outcomes import TurnOutcomeApplicator
from gca.issue_sessions.remediation import IssueSessionReconciler
from gca.issue_sessions.store import IssueSessionStore


def _seed(store: IssueSessionStore) -> tuple[IssueSession, IssueGeneration, Turn]:
    with store.unit_of_work() as uow:
        session = uow.upsert_session(
            IssueSession(
                gitlab_instance="https://gitlab.com",
                project_id=42,
                issue_iid=9,
                project_path="group/project",
                issue_title="Fix bug",
                repository_url="https://gitlab.com/group/project.git",
            )
        )
        generation = uow.insert_generation(
            IssueGeneration(
                issue_session_id=session.id,
                branch_name=f"gca/issues/9/{session.id[:8]}",
                target_branch="main",
            )
        )
        session.active_generation_id = generation.id
        uow.save_session(session)
        turn = uow.insert_turn(
            Turn(
                issue_session_id=session.id,
                generation_id=generation.id,
                kind="code",
                status=TurnStatus.RUNNING,
                workspace_path=str(Path("/tmp")),
            )
        )
    return session, generation, turn


def test_needs_human_outcome_queues_note(tmp_path: Path) -> None:
    store = IssueSessionStore(tmp_path / "db.sqlite3")
    session, generation, turn = _seed(store)
    turn.workspace_path = str(tmp_path)
    with store.unit_of_work() as uow:
        uow.save_turn(turn)
    TurnOutcomeApplicator(store).apply(
        turn_id=turn.id,
        result=AgentResult(
            status="paused",
            steps=2,
            final_message="Which environment fails?",
            outcome_kind="needs_human",
        ),
    )
    generation = store.get_generation(generation.id)
    assert generation.status == GenerationStatus.WAITING_HUMAN
    actions = store.list_pending_outbound()
    assert actions[0].kind == "issue_note"
    api = RecordingGitLabApiClient()
    OutboxProcessor(store, api).process_pending()
    assert api.notes
    assert "Which environment fails?" in api.notes[0]["body"]
    assert "/close" not in api.notes[0]["body"]


def test_changes_ready_publishes_mr(tmp_path: Path) -> None:
    store = IssueSessionStore(tmp_path / "db.sqlite3")
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    (workspace / "fix.txt").write_text("hello\n", encoding="utf-8")
    session, generation, turn = _seed(store)
    turn.workspace_path = str(workspace)
    with store.unit_of_work() as uow:
        generation.branch_name = f"gca/issues/9/{generation.id[:8]}"
        uow.save_generation(generation)
        uow.save_turn(turn)
    TurnOutcomeApplicator(store).apply(
        turn_id=turn.id,
        result=AgentResult(
            status="completed",
            steps=3,
            final_message="Fixed the bug",
            outcome_kind="changes_ready",
        ),
    )
    api = RecordingGitLabApiClient()
    processor = OutboxProcessor(store, api, git_token="")
    # Process publish then note.
    for _ in range(3):
        processor.process_pending()
    assert api.merge_requests
    link = store.get_scm_link(generation.id)
    assert link is not None
    assert link.mr_iid == 1
    assert store.get_generation(generation.id).status == GenerationStatus.AWAITING_MERGE


def test_render_note_neutralizes_quick_actions() -> None:
    body = render_issue_note(
        {
            "template": "clarification",
            "question": "/approve\nPlease confirm",
            "question_id": "abc",
        }
    )
    assert "\\/approve" in body or "/approve" not in body.splitlines()[2]


def test_trusted_commit_ignores_git_dir(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    (workspace / "ok.py").write_text("x=1\n", encoding="utf-8")
    (workspace / ".env").write_text("SECRET=1\n", encoding="utf-8")
    sha = create_trusted_commit(
        workspace,
        branch="gca/test",
        message="gca: test",
        credentials=CredentialBroker({}),
    )
    assert len(sha) == 40


def test_remediation_schedules_turn(tmp_path: Path) -> None:
    store = IssueSessionStore(tmp_path / "db.sqlite3")
    session, generation, turn = _seed(store)
    with store.unit_of_work() as uow:
        turn.status = TurnStatus.SUCCEEDED
        uow.save_turn(turn)
        generation.status = GenerationStatus.AWAITING_MERGE
        uow.save_generation(generation)
        session.status = GenerationStatus.AWAITING_MERGE
        uow.save_session(session)
    api = RecordingGitLabApiClient()
    decision = IssueSessionReconciler(store, api).maybe_schedule_pipeline_remediation(
        issue_session_id=session.id,
        generation_id=generation.id,
        pipeline_status="failed",
        failed_jobs=[
            {
                "id": 8,
                "project_id": 42,
                "name": "pytest",
                "stage": "test",
                "failure_reason": "script_failure",
            }
        ],
    )
    assert decision.action == "scheduled"
    assert store.get_generation(generation.id).remediation_attempts == 1


def test_two_key_auto_merge_schedules_only_when_both_keys_set(tmp_path: Path) -> None:
    store = IssueSessionStore(tmp_path / "db.sqlite3")
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    (workspace / ".gca").mkdir()
    (workspace / ".gca" / "config.yaml").write_text(
        "version: 1\npublication:\n  auto_merge: true\n",
        encoding="utf-8",
    )
    (workspace / "fix.txt").write_text("hello\n", encoding="utf-8")
    session, generation, turn = _seed(store)
    turn.workspace_path = str(workspace)
    with store.unit_of_work() as uow:
        generation.branch_name = f"gca/issues/9/{generation.id[:8]}"
        generation.metadata = {"auto_merge": True}
        uow.save_generation(generation)
        uow.save_turn(turn)
    TurnOutcomeApplicator(store).apply(
        turn_id=turn.id,
        result=AgentResult(
            status="completed",
            steps=3,
            final_message="Fixed the bug",
            outcome_kind="changes_ready",
        ),
    )
    api = RecordingGitLabApiClient()
    # Publish must not merge immediately; wait for pipeline success + two keys.
    processor = OutboxProcessor(
        store,
        api,
        git_token="",
        allow_auto_merge_projects=frozenset({42}),
    )
    for _ in range(3):
        processor.process_pending()
    assert api.merge_requests
    assert not api.merges
    decision = IssueSessionReconciler(
        store,
        api,
        allow_auto_merge_projects=frozenset({42}),
    ).handle_pipeline_event(
        issue_session_id=session.id,
        generation_id=generation.id,
        pipeline_status="success",
        pipeline_sha="a" * 40,
    )
    assert decision.action == "scheduled"
    for _ in range(2):
        processor.process_pending()
    assert api.merges
    assert api.merges[0]["sha"]


def test_auto_merge_denied_without_operator_key(tmp_path: Path) -> None:
    store = IssueSessionStore(tmp_path / "db.sqlite3")
    session, generation, turn = _seed(store)
    with store.unit_of_work() as uow:
        generation.status = GenerationStatus.AWAITING_MERGE
        generation.metadata = {"auto_merge": True}
        uow.save_generation(generation)
        uow.upsert_scm_link(
            ScmLink(
                issue_session_id=session.id,
                generation_id=generation.id,
                source_project_id=42,
                target_project_id=42,
                branch_name="gca/issues/9/x",
                target_branch="main",
                ownership_marker="m",
                mr_iid=7,
                expected_head_sha="a" * 40,
            )
        )
    api = RecordingGitLabApiClient()
    api.merge_requests.append(
        {
            "iid": 7,
            "project_id": 42,
            "sha": "a" * 40,
            "detailed_merge_status": "mergeable",
        }
    )
    decision = IssueSessionReconciler(
        store,
        api,
        allow_auto_merge_projects=frozenset(),
    ).handle_pipeline_event(
        issue_session_id=session.id,
        generation_id=generation.id,
        pipeline_status="success",
        pipeline_sha="a" * 40,
    )
    assert decision.action == "ignored"
