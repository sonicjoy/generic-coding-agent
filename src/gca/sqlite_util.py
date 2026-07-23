"""Shared SQLite helpers for concurrent process bring-up."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")


def connect(path: str | bytes | Path, *, timeout: float = 30) -> sqlite3.Connection:
    """Open a SQLite connection with a busy timeout for multi-process use."""

    connection = sqlite3.connect(str(path), timeout=timeout, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def retry_locked(operation: Callable[[], T], *, attempts: int = 8) -> T:
    """Retry ``operation`` when SQLite reports the database is locked."""

    delay = 0.05
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(attempts):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise
            last_error = exc
            if attempt >= attempts - 1:
                break
            time.sleep(delay)
            delay = min(delay * 2, 1.0)
    assert last_error is not None
    raise last_error
