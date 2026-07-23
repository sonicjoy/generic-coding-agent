from __future__ import annotations

from pathlib import Path

import pytest

from gca.providers.base import Message
from gca.session import (
    STATUS_COMPLETED,
    AgentRunRecord,
    Session,
    SessionStore,
    WorkflowState,
)


def test_create_save_load_roundtrip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    session = store.create("do a thing")
    session.messages.append(Message(role="user", content="hello"))
    session.plan = "step 1"
    session.status = STATUS_COMPLETED
    session.step_count = 3
    session.workflow = WorkflowState(
        name="feature",
        phase="review",
        complexity="large",
        model_bindings={"planning": "strong"},
        artifacts={"plan": "step 1"},
    )
    session.agent_runs.append(
        AgentRunRecord(
            phase="planning",
            model="strong",
            messages=[Message(role="assistant", content="planned")],
            status=STATUS_COMPLETED,
            step_count=1,
        )
    )
    store.save(session)

    loaded = store.load(session.id)
    assert loaded.task == "do a thing"
    assert loaded.plan == "step 1"
    assert loaded.status == STATUS_COMPLETED
    assert loaded.step_count == 3
    assert loaded.messages[0].content == "hello"
    assert loaded.workflow is not None
    assert loaded.workflow.artifacts["plan"] == "step 1"
    assert loaded.agent_runs[0].model == "strong"


def test_list_sorted_by_updated(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    a = store.create("task a")
    b = store.create("task b")
    store.save(b)  # b updated most recently
    summaries = store.list()
    assert {s["id"] for s in summaries} == {a.id, b.id}
    assert summaries[0]["id"] == b.id


def test_loads_legacy_session_without_workflow_fields() -> None:
    session = Session.from_dict({"id": "legacy", "task": "old task"})

    assert session.schema_version == 0
    assert session.workflow is None
    assert session.agent_runs == []


def test_session_store_rejects_path_traversal_ids(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")

    with pytest.raises(ValueError, match="invalid characters"):
        store.load("../../outside")
