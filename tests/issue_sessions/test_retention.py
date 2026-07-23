"""Tests for workspace and structured-log retention."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gca.issue_sessions.models import IssueSession
from gca.issue_sessions.retention import RetentionJanitor
from gca.issue_sessions.store import IssueSessionStore


def test_retention_deletes_expired_workspaces_and_events(tmp_path: Path) -> None:
    store = IssueSessionStore(tmp_path / "db.sqlite3")
    root = tmp_path / "workspaces"
    root.mkdir()
    keep = root / "active"
    drop = root / "old"
    keep.mkdir()
    drop.mkdir()
    now = datetime.now(timezone.utc)
    (keep / "retention.json").write_text(
        json.dumps({"status": "running", "updated_at": now.isoformat()}),
        encoding="utf-8",
    )
    (drop / "retention.json").write_text(
        json.dumps(
            {
                "status": "succeeded",
                "updated_at": (now - timedelta(hours=2)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
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
        old = uow.append_event(
            issue_session_id=session.id,
            kind="audit",
            payload={"age": "old"},
        )
        recent = uow.append_event(
            issue_session_id=session.id,
            kind="audit",
            payload={"age": "recent"},
        )
        uow.connection.execute(
            "UPDATE session_events SET created_at = ? WHERE id = ?",
            ((now - timedelta(days=40)).isoformat(), old.id),
        )
        # Keep the recent event's created_at near now (append_event default).
        _ = recent
    result = RetentionJanitor(
        store,
        workspace_root=root,
        workspace_retention_seconds=3600,
        log_retention_seconds=86400 * 30,
    ).run()
    assert result["deleted_workspaces"] == 1
    assert not drop.exists()
    assert keep.exists()
    assert result["deleted_events"] >= 1
    remaining = store.list_events(session.id, after_seq=0, limit=10)
    assert len(remaining) == 1
    assert remaining[0].payload.get("age") == "recent"
