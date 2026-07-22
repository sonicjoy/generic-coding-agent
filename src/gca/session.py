"""Session management: persistent, resumable agent runs.

Each agent run is a :class:`Session` capturing the task, the full conversation,
the current plan, a step counter, and a lifecycle status. Sessions are persisted
as JSON so a run can be paused and resumed (e.g. to work on git issues
continuously). :class:`SessionStore` handles create / save / load / list.
"""

from __future__ import annotations

import json
import re
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
class AgentRunRecord:
    """Persisted conversation and outcome for one workflow agent."""

    phase: str
    model: str
    messages: list[Message] = field(default_factory=list)
    status: str = STATUS_ACTIVE
    step_count: int = 0
    final_message: str = ""
    inflight_tool_call_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize this phase run."""

        return {
            "phase": self.phase,
            "model": self.model,
            "messages": [message.to_dict() for message in self.messages],
            "status": self.status,
            "step_count": self.step_count,
            "final_message": self.final_message,
            "inflight_tool_call_id": self.inflight_tool_call_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentRunRecord:
        """Load a phase run from persisted data."""

        return cls(
            phase=str(data.get("phase", "")),
            model=str(data.get("model", "")),
            messages=[Message.from_dict(message) for message in data.get("messages", [])],
            status=str(data.get("status", STATUS_ACTIVE)),
            step_count=int(data.get("step_count", 0)),
            final_message=str(data.get("final_message", "")),
            inflight_tool_call_id=str(data.get("inflight_tool_call_id", "")),
        )


@dataclass
class WorkflowState:
    """Durable orchestration state for a multi-agent workflow."""

    name: str = ""
    phase: str = ""
    complexity: str = ""
    complexity_score: int = 0
    complexity_signals: list[str] = field(default_factory=list)
    review_cycles: int = 0
    max_review_cycles: int = 2
    model_bindings: dict[str, str] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    provider_states: dict[str, dict[str, Any]] = field(default_factory=dict)
    registry_fingerprint: str = ""
    policy_fingerprint: str = ""
    config_fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize workflow state."""

        return {
            "name": self.name,
            "phase": self.phase,
            "complexity": self.complexity,
            "complexity_score": self.complexity_score,
            "complexity_signals": self.complexity_signals,
            "review_cycles": self.review_cycles,
            "max_review_cycles": self.max_review_cycles,
            "model_bindings": self.model_bindings,
            "artifacts": self.artifacts,
            "provider_states": self.provider_states,
            "registry_fingerprint": self.registry_fingerprint,
            "policy_fingerprint": self.policy_fingerprint,
            "config_fingerprint": self.config_fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowState:
        """Load workflow state from persisted data."""

        provider_states = {
            str(name): dict(state) for name, state in data.get("provider_states", {}).items()
        }
        return cls(
            name=str(data.get("name", "")),
            phase=str(data.get("phase", "")),
            complexity=str(data.get("complexity", "")),
            complexity_score=int(data.get("complexity_score", 0)),
            complexity_signals=[str(signal) for signal in data.get("complexity_signals", [])],
            review_cycles=int(data.get("review_cycles", 0)),
            max_review_cycles=int(data.get("max_review_cycles", 2)),
            model_bindings={
                str(role): str(model) for role, model in data.get("model_bindings", {}).items()
            },
            artifacts={str(name): str(value) for name, value in data.get("artifacts", {}).items()},
            provider_states=provider_states,
            registry_fingerprint=str(data.get("registry_fingerprint", "")),
            policy_fingerprint=str(data.get("policy_fingerprint", "")),
            config_fingerprint=str(data.get("config_fingerprint", "")),
        )


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
    schema_version: int = 1
    workflow: WorkflowState | None = None
    agent_runs: list[AgentRunRecord] = field(default_factory=list)
    provider_state: dict[str, Any] = field(default_factory=dict)
    active_model: str = ""
    final_message: str = ""
    inflight_tool_call_id: str = ""

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
            "schema_version": self.schema_version,
            "workflow": self.workflow.to_dict() if self.workflow is not None else None,
            "agent_runs": [run.to_dict() for run in self.agent_runs],
            "provider_state": self.provider_state,
            "active_model": self.active_model,
            "final_message": self.final_message,
            "inflight_tool_call_id": self.inflight_tool_call_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        workflow_data = data.get("workflow")
        return cls(
            id=str(data["id"]),
            task=str(data.get("task", "")),
            messages=[Message.from_dict(m) for m in data.get("messages", [])],
            plan=str(data.get("plan", "")),
            status=str(data.get("status", STATUS_ACTIVE)),
            step_count=int(data.get("step_count", 0)),
            created_at=str(data.get("created_at", _now())),
            updated_at=str(data.get("updated_at", _now())),
            schema_version=int(data.get("schema_version", 0)),
            workflow=(
                WorkflowState.from_dict(workflow_data) if isinstance(workflow_data, dict) else None
            ),
            agent_runs=[AgentRunRecord.from_dict(run) for run in data.get("agent_runs", [])],
            provider_state=dict(data.get("provider_state", {})),
            active_model=str(data.get("active_model", "")),
            final_message=str(data.get("final_message", "")),
            inflight_tool_call_id=str(data.get("inflight_tool_call_id", "")),
        )


class SessionStore:
    """Filesystem-backed store for sessions (one JSON file per session)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        if re.fullmatch(r"[A-Za-z0-9_-]+", session_id) is None:
            raise ValueError("session_id contains invalid characters")
        return self.root / f"{session_id}.json"

    def create(self, task: str) -> Session:
        session = Session(id=uuid.uuid4().hex[:12], task=task)
        self.save(session)
        return session

    def save(self, session: Session) -> None:
        session.touch()
        path = self._path(session.id)
        temporary = self.root / f".{session.id}.{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_text(
                json.dumps(session.to_dict(), indent=2),
                encoding="utf-8",
            )
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink()

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
