"""Serializable job, repository, and publication models."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
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
        url = data.get("url")
        ref = data.get("ref", "main")
        depth = data.get("shallow_depth", 1)
        if not isinstance(url, str) or not url.strip():
            raise ValueError("repository.url must be a non-empty string")
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError("repository.ref must be a non-empty string")
        if isinstance(depth, bool) or not isinstance(depth, int):
            raise ValueError("repository.shallow_depth must be an integer")
        return cls(
            url=url,
            ref=ref,
            shallow_depth=depth,
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
        provider = data.get("provider")
        base_ref = data.get("base_ref", "main")
        branch_prefix = data.get("branch_prefix", "gca/")
        draft = data.get("draft", False)
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("publication.provider must be a non-empty string")
        if not isinstance(base_ref, str) or not base_ref.strip():
            raise ValueError("publication.base_ref must be a non-empty string")
        if not isinstance(branch_prefix, str) or not branch_prefix.strip():
            raise ValueError("publication.branch_prefix must be a non-empty string")
        if not isinstance(draft, bool):
            raise ValueError("publication.draft must be a boolean")
        return cls(
            provider=provider,
            base_ref=base_ref,
            branch_prefix=branch_prefix,
            draft=draft,
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
        task = data.get("task")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("run spec requires a non-empty task string")
        repository_raw = data.get("repository")
        if not isinstance(repository_raw, Mapping):
            raise ValueError("run spec requires a repository mapping")
        publication_raw = data.get("publication")
        if publication_raw is not None and not isinstance(publication_raw, Mapping):
            raise ValueError("publication must be a mapping")
        workflow = data.get("workflow")
        if workflow is not None and not isinstance(workflow, str):
            raise ValueError("workflow must be a string")
        max_steps = data.get("max_steps")
        if max_steps is not None and (
            isinstance(max_steps, bool) or not isinstance(max_steps, int)
        ):
            raise ValueError("max_steps must be an integer")
        labels_raw = data.get("labels", {})
        if not isinstance(labels_raw, Mapping):
            raise ValueError("labels must be a mapping")
        if not all(
            isinstance(key, str) and isinstance(value, str) for key, value in labels_raw.items()
        ):
            raise ValueError("labels must be a string mapping")
        return cls(
            task=task,
            repository=RepositorySpec.from_dict(dict(repository_raw)),
            workflow=workflow,
            max_steps=max_steps,
            publication=(
                PublicationTarget.from_dict(dict(publication_raw))
                if isinstance(publication_raw, Mapping)
                else None
            ),
            labels=dict(labels_raw),
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
    result_summary: str = ""
    last_error: str = ""
    llm_usage: dict[str, Any] = field(default_factory=dict)
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
            "result_summary": self.result_summary,
            "last_error": self.last_error,
            "llm_usage": dict(self.llm_usage),
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

        usage = data.get("llm_usage")
        return cls(
            id=str(data["id"]),
            status=JobStatus(str(data.get("status", JobStatus.QUEUED.value))),
            idempotency_key=(
                str(data["idempotency_key"]) if data.get("idempotency_key") is not None else None
            ),
            attempt=int(data.get("attempt", 0)),
            max_attempts=int(data.get("max_attempts", 3)),
            run_spec=RunSpec.from_dict(dict(data["run_spec"])),
            session_id=(str(data["session_id"]) if data.get("session_id") else None),
            workspace_path=(str(data["workspace_path"]) if data.get("workspace_path") else None),
            publication=dict(data.get("publication", {})),
            result_summary=str(data.get("result_summary", "")),
            last_error=str(data.get("last_error", "")),
            llm_usage=dict(usage) if isinstance(usage, dict) else {},
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
