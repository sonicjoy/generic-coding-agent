from __future__ import annotations

from pathlib import Path

from gca.providers.base import Message
from gca.session import STATUS_COMPLETED, SessionStore


def test_create_save_load_roundtrip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    session = store.create("do a thing")
    session.messages.append(Message(role="user", content="hello"))
    session.plan = "step 1"
    session.status = STATUS_COMPLETED
    session.step_count = 3
    store.save(session)

    loaded = store.load(session.id)
    assert loaded.task == "do a thing"
    assert loaded.plan == "step 1"
    assert loaded.status == STATUS_COMPLETED
    assert loaded.step_count == 3
    assert loaded.messages[0].content == "hello"


def test_list_sorted_by_updated(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    a = store.create("task a")
    b = store.create("task b")
    store.save(b)  # b updated most recently
    summaries = store.list()
    assert {s["id"] for s in summaries} == {a.id, b.id}
    assert summaries[0]["id"] == b.id
