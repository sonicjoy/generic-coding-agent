from __future__ import annotations

import threading
from pathlib import Path

from gca.issue_sessions.store import IssueSessionStore
from gca.jobs.store import SqliteJobStore
from gca.sqlite_util import retry_locked


def test_concurrent_job_store_init_does_not_raise(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite3"
    errors: list[BaseException] = []

    def open_store() -> None:
        try:
            SqliteJobStore(path)
        except BaseException as exc:  # noqa: BLE001 - collect for assertion
            errors.append(exc)

    threads = [threading.Thread(target=open_store) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    SqliteJobStore(path)


def test_concurrent_issue_session_store_init_does_not_raise(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite3"
    errors: list[BaseException] = []

    def open_store() -> None:
        try:
            IssueSessionStore(path)
        except BaseException as exc:  # noqa: BLE001 - collect for assertion
            errors.append(exc)

    threads = [threading.Thread(target=open_store) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []


def test_retry_locked_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            import sqlite3

            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert retry_locked(flaky, attempts=5) == "ok"
    assert calls["n"] == 3
