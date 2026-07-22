"""Serializable job, repository, and publication models."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp."""

    return datetime.now(timezone.utc).isoformat()


class JobStatus(str, Enum):
    """Hosted job lifecycle states."""

    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    PUBLISHING = "publishing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class RepositorySpec:
    """Remote repository checkout requested for a job."""

    url: str
    ref: str = "main"
    shallow_depth: int = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RepositorySpec:
        return cls(
            url=str(data["url"]),
            ref=str(data.get("ref", "main")),
            shallow_depth=int(data.get("shallow_depth", 1)),
        )


@dataclass(frozen=True)
class PublicationTarget:
    """Optional SCM publication request."""

    provider: str
    base_ref: str = "main"
    branch_prefix: str = "gca/"
    draft: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PublicationTarget:
        return cls(
            provider=str(data["provider"]),
            base_ref=str(data.get("base_ref", "main")),
            branch_prefix=str(data.get("branch_prefix", "gca/")),
            draft=bool(data.get("draft", False)),
        )


@dataclass(frozen=True)
class RunSpec:
    """Normalized request consumed by the generic job runner."""

    task: str
    repository: RepositorySpec
    workflow: str | None = None
    max_steps: int | None = None
    publication: PublicationTarget | None = None
    labels: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunSpec:
        repository_raw = data.get("repository")
        if not isinstance(repository_raw, dict):
            raise ValueError("run spec requires a repository mapping")
        publication_raw = data.get("publication")
        return cls(
            task=str(data["task"]),
            repository=RepositorySpec.from_dict(repository_raw),
            workflow=(str(data["workflow"]) if data.get("workflow") is not None else None),
            max_steps=(int(data["max_steps"]) if data.get("max_steps") is not None else None),
            publication=(
                PublicationTarget.from_dict(publication_raw)
                if isinstance(publication_raw, dict)
                else None
            ),
            labels={str(key): str(value) for key, value in data.get("labels", {}).items()},
        )


@dataclass
class Job:
    """Durable state for one repository agent execution."""

    run_spec: RunSpec
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: JobStatus = JobStatus.QUEUED
    idempotency_key: str | None = None
    attempt: int = 0
    max_attempts: int = 3
    session_id: str | None = None
    workspace_path: str | None = None
    publication: dict[str, Any] = field(default_factory=dict)
    last_error: str = ""
    lease_owner: str | None = None
    lease_expires_at: float | None = None
    not_before: float = 0.0
    version: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        """Serialize a job for durable storage and APIs."""

        return {
            "id": self.id,
            "status": self.status.value,
            "idempotency_key": self.idempotency_key,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "run_spec": self.run_spec.to_dict(),
            "session_id": self.session_id,
            "workspace_path": self.workspace_path,
            "publication": self.publication,
            "last_error": self.last_error,
            "lease_owner": self.lease_owner,
            "lease_expires_at": self.lease_expires_at,
            "not_before": self.not_before,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        """Deserialize a persisted job."""

        return cls(
            id=str(data["id"]),
            status=JobStatus(str(data.get("status", JobStatus.QUEUED.value))),
            idempotency_key=(
                str(data["idempotency_key"])
                if data.get("idempotency_key") is not None
                else None
            ),
            attempt=int(data.get("attempt", 0)),
            max_attempts=int(data.get("max_attempts", 3)),
            run_spec=RunSpec.from_dict(dict(data["run_spec"])),
            session_id=(str(data["session_id"]) if data.get("session_id") else None),
            workspace_path=(
                str(data["workspace_path"]) if data.get("workspace_path") else None
            ),
            publication=dict(data.get("publication", {})),
            last_error=str(data.get("last_error", "")),
            lease_owner=(str(data["lease_owner"]) if data.get("lease_owner") else None),
            lease_expires_at=(
                float(data["lease_expires_at"])
                if data.get("lease_expires_at") is not None
                else None
            ),
            not_before=float(data.get("not_before", 0.0)),
            version=int(data.get("version", 0)),
            created_at=str(data.get("created_at", utc_now())),
            updated_at=str(data.get("updated_at", utc_now())),
        )
