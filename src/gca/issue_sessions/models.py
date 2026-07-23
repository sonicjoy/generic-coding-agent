"""Serializable models for durable issue sessions, generations, and turns."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from gca.jobs.models import utc_now


class GenerationStatus(str, Enum):
    """Lifecycle status for one issue-session generation."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    PUBLISHING = "publishing"
    AWAITING_MERGE = "awaiting_merge"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TurnStatus(str, Enum):
    """Lifecycle status for one agent turn."""

    QUEUED = "queued"
    RUNNING = "running"
    PAUSED_BUDGET = "paused_budget"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OutboundActionStatus(str, Enum):
    """Lifecycle status for one durable outbox side effect."""

    PENDING = "pending"
    LEASED = "leased"
    RECONCILING = "reconciling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class WaitReason(str, Enum):
    """Typed reason a generation is waiting for a human or operator."""

    CLARIFICATION = "clarification"
    NO_SAFE_CHANGE = "no_safe_change"
    BUDGET_EXHAUSTED = "budget_exhausted"
    REMEDIATION_EXHAUSTED = "remediation_exhausted"
    MR_CLOSED = "mr_closed"
    EXTERNAL_CHANGE = "external_change"
    AMBIGUOUS_SIDE_EFFECT = "ambiguous_side_effect"
    PERMISSION_BLOCKER = "permission_blocker"
    LOST_FENCING = "lost_fencing"


class TurnOutcomeKind(str, Enum):
    """Structured outcomes produced by a hosted agent turn."""

    CHANGES_READY = "changes_ready"
    NEEDS_HUMAN = "needs_human"
    NO_SAFE_CHANGE = "no_safe_change"
    FAILED = "failed"
    BUDGET_EXHAUSTED = "budget_exhausted"


class MergeReason(str, Enum):
    """How an issue session completed via merge."""

    MANAGED = "managed"
    EXTERNAL_MUTATION = "external_mutation"
    EXTERNAL_OLD_GENERATION = "external_old_generation"


@dataclass
class IssueSession:
    """Durable aggregate keyed by GitLab instance, project, and issue IID."""

    gitlab_instance: str
    project_id: int
    issue_iid: int
    project_path: str
    issue_title: str
    repository_url: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: GenerationStatus = GenerationStatus.QUEUED
    active_generation_id: str | None = None
    registration_id: str = ""
    trigger_label: str = "gca-run"
    version: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "gitlab_instance": self.gitlab_instance,
            "project_id": self.project_id,
            "issue_iid": self.issue_iid,
            "project_path": self.project_path,
            "issue_title": self.issue_title,
            "repository_url": self.repository_url,
            "status": self.status.value,
            "active_generation_id": self.active_generation_id,
            "registration_id": self.registration_id,
            "trigger_label": self.trigger_label,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IssueSession:
        return cls(
            id=str(data["id"]),
            gitlab_instance=str(data["gitlab_instance"]),
            project_id=int(data["project_id"]),
            issue_iid=int(data["issue_iid"]),
            project_path=str(data["project_path"]),
            issue_title=str(data.get("issue_title", "")),
            repository_url=str(data["repository_url"]),
            status=GenerationStatus(str(data.get("status", GenerationStatus.QUEUED.value))),
            active_generation_id=(
                str(data["active_generation_id"]) if data.get("active_generation_id") else None
            ),
            registration_id=str(data.get("registration_id", "")),
            trigger_label=str(data.get("trigger_label", "gca-run")),
            version=int(data.get("version", 0)),
            created_at=str(data.get("created_at", utc_now())),
            updated_at=str(data.get("updated_at", utc_now())),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class IssueGeneration:
    """One attempt generation owned by an issue session."""

    issue_session_id: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: GenerationStatus = GenerationStatus.QUEUED
    wait_reason: WaitReason | None = None
    target_branch: str = "main"
    target_base_sha: str = ""
    policy_fingerprint: str = ""
    branch_name: str = ""
    steps_consumed: int = 0
    max_steps: int = 100
    remediation_attempts: int = 0
    max_remediation_attempts: int = 3
    outstanding_question_id: str | None = None
    summary: str = ""
    merge_reason: MergeReason | None = None
    lease_epoch: int = 0
    cancel_requested: bool = False
    version: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "issue_session_id": self.issue_session_id,
            "status": self.status.value,
            "wait_reason": self.wait_reason.value if self.wait_reason else None,
            "target_branch": self.target_branch,
            "target_base_sha": self.target_base_sha,
            "policy_fingerprint": self.policy_fingerprint,
            "branch_name": self.branch_name,
            "steps_consumed": self.steps_consumed,
            "max_steps": self.max_steps,
            "remediation_attempts": self.remediation_attempts,
            "max_remediation_attempts": self.max_remediation_attempts,
            "outstanding_question_id": self.outstanding_question_id,
            "summary": self.summary,
            "merge_reason": self.merge_reason.value if self.merge_reason else None,
            "lease_epoch": self.lease_epoch,
            "cancel_requested": self.cancel_requested,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IssueGeneration:
        wait_reason = data.get("wait_reason")
        merge_reason = data.get("merge_reason")
        return cls(
            id=str(data["id"]),
            issue_session_id=str(data["issue_session_id"]),
            status=GenerationStatus(str(data.get("status", GenerationStatus.QUEUED.value))),
            wait_reason=WaitReason(str(wait_reason)) if wait_reason else None,
            target_branch=str(data.get("target_branch", "main")),
            target_base_sha=str(data.get("target_base_sha", "")),
            policy_fingerprint=str(data.get("policy_fingerprint", "")),
            branch_name=str(data.get("branch_name", "")),
            steps_consumed=int(data.get("steps_consumed", 0)),
            max_steps=int(data.get("max_steps", 100)),
            remediation_attempts=int(data.get("remediation_attempts", 0)),
            max_remediation_attempts=int(data.get("max_remediation_attempts", 3)),
            outstanding_question_id=(
                str(data["outstanding_question_id"])
                if data.get("outstanding_question_id")
                else None
            ),
            summary=str(data.get("summary", "")),
            merge_reason=MergeReason(str(merge_reason)) if merge_reason else None,
            lease_epoch=int(data.get("lease_epoch", 0)),
            cancel_requested=bool(data.get("cancel_requested", False)),
            version=int(data.get("version", 0)),
            created_at=str(data.get("created_at", utc_now())),
            updated_at=str(data.get("updated_at", utc_now())),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class Turn:
    """One bounded agent turn belonging to a generation."""

    issue_session_id: str
    generation_id: str
    kind: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: TurnStatus = TurnStatus.QUEUED
    job_id: str | None = None
    agent_session_id: str | None = None
    workspace_path: str | None = None
    max_steps: int = 25
    steps_consumed: int = 0
    lease_epoch: int = 0
    outcome_kind: TurnOutcomeKind | None = None
    outcome_summary: str = ""
    question_id: str | None = None
    version: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "issue_session_id": self.issue_session_id,
            "generation_id": self.generation_id,
            "kind": self.kind,
            "status": self.status.value,
            "job_id": self.job_id,
            "agent_session_id": self.agent_session_id,
            "workspace_path": self.workspace_path,
            "max_steps": self.max_steps,
            "steps_consumed": self.steps_consumed,
            "lease_epoch": self.lease_epoch,
            "outcome_kind": self.outcome_kind.value if self.outcome_kind else None,
            "outcome_summary": self.outcome_summary,
            "question_id": self.question_id,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Turn:
        outcome = data.get("outcome_kind")
        return cls(
            id=str(data["id"]),
            issue_session_id=str(data["issue_session_id"]),
            generation_id=str(data["generation_id"]),
            kind=str(data.get("kind", "code")),
            status=TurnStatus(str(data.get("status", TurnStatus.QUEUED.value))),
            job_id=(str(data["job_id"]) if data.get("job_id") else None),
            agent_session_id=(
                str(data["agent_session_id"]) if data.get("agent_session_id") else None
            ),
            workspace_path=(str(data["workspace_path"]) if data.get("workspace_path") else None),
            max_steps=int(data.get("max_steps", 25)),
            steps_consumed=int(data.get("steps_consumed", 0)),
            lease_epoch=int(data.get("lease_epoch", 0)),
            outcome_kind=TurnOutcomeKind(str(outcome)) if outcome else None,
            outcome_summary=str(data.get("outcome_summary", "")),
            question_id=(str(data["question_id"]) if data.get("question_id") else None),
            version=int(data.get("version", 0)),
            created_at=str(data.get("created_at", utc_now())),
            updated_at=str(data.get("updated_at", utc_now())),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class InboundEvent:
    """Normalized inbound GitLab event persisted before scheduling."""

    provider: str
    gitlab_instance: str
    project_id: int
    delivery_id: str
    event_uuid: str
    event_type: str
    action: str
    object_key: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    issue_session_id: str | None = None
    generation_id: str | None = None
    consumed_by_turn_id: str | None = None
    actor_id: int | None = None
    actor_username: str = ""
    authorized: bool = False
    authorization_reason: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InboundEvent:
        return cls(
            id=str(data["id"]),
            provider=str(data["provider"]),
            gitlab_instance=str(data["gitlab_instance"]),
            project_id=int(data["project_id"]),
            delivery_id=str(data["delivery_id"]),
            event_uuid=str(data.get("event_uuid", "")),
            event_type=str(data["event_type"]),
            action=str(data.get("action", "")),
            object_key=str(data["object_key"]),
            issue_session_id=(
                str(data["issue_session_id"]) if data.get("issue_session_id") else None
            ),
            generation_id=(str(data["generation_id"]) if data.get("generation_id") else None),
            consumed_by_turn_id=(
                str(data["consumed_by_turn_id"]) if data.get("consumed_by_turn_id") else None
            ),
            actor_id=(int(data["actor_id"]) if data.get("actor_id") is not None else None),
            actor_username=str(data.get("actor_username", "")),
            authorized=bool(data.get("authorized", False)),
            authorization_reason=str(data.get("authorization_reason", "")),
            payload=dict(data.get("payload", {})),
            created_at=str(data.get("created_at", utc_now())),
        )


@dataclass
class ScmLink:
    """Verified SCM ownership metadata for one generation."""

    issue_session_id: str
    generation_id: str
    source_project_id: int
    target_project_id: int
    branch_name: str
    target_branch: str
    ownership_marker: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    bot_author_id: int | None = None
    mr_iid: int | None = None
    mr_global_id: str | None = None
    mr_url: str | None = None
    expected_head_sha: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScmLink:
        return cls(
            id=str(data["id"]),
            issue_session_id=str(data["issue_session_id"]),
            generation_id=str(data["generation_id"]),
            source_project_id=int(data["source_project_id"]),
            target_project_id=int(data["target_project_id"]),
            branch_name=str(data["branch_name"]),
            target_branch=str(data["target_branch"]),
            ownership_marker=str(data["ownership_marker"]),
            bot_author_id=(int(data["bot_author_id"]) if data.get("bot_author_id") else None),
            mr_iid=(int(data["mr_iid"]) if data.get("mr_iid") is not None else None),
            mr_global_id=(str(data["mr_global_id"]) if data.get("mr_global_id") else None),
            mr_url=(str(data["mr_url"]) if data.get("mr_url") else None),
            expected_head_sha=str(data.get("expected_head_sha", "")),
            created_at=str(data.get("created_at", utc_now())),
            updated_at=str(data.get("updated_at", utc_now())),
        )


@dataclass
class OutboundAction:
    """Durable outbox intent for one GitLab/service side effect."""

    issue_session_id: str
    generation_id: str
    kind: str
    effect_key: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    turn_id: str | None = None
    status: OutboundActionStatus = OutboundActionStatus.PENDING
    lease_epoch: int = 0
    payload: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    last_error: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "issue_session_id": self.issue_session_id,
            "generation_id": self.generation_id,
            "turn_id": self.turn_id,
            "kind": self.kind,
            "effect_key": self.effect_key,
            "status": self.status.value,
            "lease_epoch": self.lease_epoch,
            "payload": dict(self.payload),
            "result": dict(self.result),
            "attempts": self.attempts,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OutboundAction:
        return cls(
            id=str(data["id"]),
            issue_session_id=str(data["issue_session_id"]),
            generation_id=str(data["generation_id"]),
            turn_id=(str(data["turn_id"]) if data.get("turn_id") else None),
            kind=str(data["kind"]),
            effect_key=str(data["effect_key"]),
            status=OutboundActionStatus(
                str(data.get("status", OutboundActionStatus.PENDING.value))
            ),
            lease_epoch=int(data.get("lease_epoch", 0)),
            payload=dict(data.get("payload", {})),
            result=dict(data.get("result", {})),
            attempts=int(data.get("attempts", 0)),
            last_error=str(data.get("last_error", "")),
            created_at=str(data.get("created_at", utc_now())),
            updated_at=str(data.get("updated_at", utc_now())),
        )


@dataclass
class SessionEvent:
    """Append-only redacted operator/eval event."""

    issue_session_id: str
    seq: int
    kind: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    generation_id: str | None = None
    turn_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionEvent:
        return cls(
            id=str(data["id"]),
            issue_session_id=str(data["issue_session_id"]),
            generation_id=(str(data["generation_id"]) if data.get("generation_id") else None),
            turn_id=(str(data["turn_id"]) if data.get("turn_id") else None),
            seq=int(data["seq"]),
            kind=str(data["kind"]),
            payload=dict(data.get("payload", {})),
            created_at=str(data.get("created_at", utc_now())),
        )
