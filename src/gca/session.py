"""Session management: persistent, resumable agent runs.

Each agent run is a :class:`Session` capturing the task, the full conversation,
the current plan, a step counter, and a lifecycle status. Sessions are persisted
as JSON so a run can be paused and resumed (e.g. to work on git issues
continuously). :class:`SessionStore` handles create / save / load / list.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gca.providers.base import Message

# Lifecycle statuses a session may hold.
STATUS_ACTIVE = "active"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_PAUSED = "paused"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Session:
    """Durable state for a single agent run."""

    id: str
    task: str
    messages: list[Message] = field(default_factory=list)
    plan: str = ""
    status: str = STATUS_ACTIVE
    step_count: int = 0
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def touch(self) -> None:
        self.updated_at = _now()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task": self.task,
            "messages": [m.to_dict() for m in self.messages],
            "plan": self.plan,
            "status": self.status,
            "step_count": self.step_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        return cls(
            id=str(data["id"]),
            task=str(data.get("task", "")),
            messages=[Message.from_dict(m) for m in data.get("messages", [])],
            plan=str(data.get("plan", "")),
            status=str(data.get("status", STATUS_ACTIVE)),
            step_count=int(data.get("step_count", 0)),
            created_at=str(data.get("created_at", _now())),
            updated_at=str(data.get("updated_at", _now())),
        )


class SessionStore:
    """Filesystem-backed store for sessions (one JSON file per session)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.json"

    def create(self, task: str) -> Session:
        session = Session(id=uuid.uuid4().hex[:12], task=task)
        self.save(session)
        return session

    def save(self, session: Session) -> None:
        session.touch()
        self._path(session.id).write_text(json.dumps(session.to_dict(), indent=2), encoding="utf-8")

    def load(self, session_id: str) -> Session:
        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(f"no such session: {session_id}")
        return Session.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list(self) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            summaries.append(
                {
                    "id": data.get("id"),
                    "task": data.get("task"),
                    "status": data.get("status"),
                    "steps": data.get("step_count"),
                    "updated_at": data.get("updated_at"),
                }
            )
        summaries.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
        return summaries
