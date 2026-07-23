"""Cleanup hosted jobs when an SCM pull/merge request is merged."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from gca.jobs.lifecycle import JobTransitionError, transition_job
from gca.jobs.models import Job, JobStatus
from gca.jobs.store import JobStore
from gca.workspace.layout import JobWorkspace


@dataclass(frozen=True)
class MergedChangeRequest:
    """Identity of a merged pull/merge request used to locate related jobs."""

    provider: str
    project: str
    number: str
    url: str = ""
    head_ref: str = ""


@dataclass(frozen=True)
class MergeCleanupResult:
    """Summary of jobs cancelled and workspaces removed for one merge event."""

    matched_job_ids: tuple[str, ...]
    cancelled_job_ids: tuple[str, ...]
    wiped_workspaces: tuple[str, ...]


_ACTIVE = frozenset(
    {
        JobStatus.QUEUED,
        JobStatus.RUNNING,
        JobStatus.PAUSED,
        JobStatus.PUBLISHING,
    }
)


def jobs_for_merged_change_request(
    store: JobStore,
    merged: MergedChangeRequest,
    *,
    limit: int = 1000,
) -> list[Job]:
    """Return jobs linked to ``merged`` via labels, publication URL, or head branch."""

    matches: list[Job] = []
    for job in store.list(limit=limit):
        if _job_matches_merged(job, merged):
            matches.append(job)
    return matches


def cleanup_jobs_for_merged_change_request(
    store: JobStore,
    merged: MergedChangeRequest,
    *,
    workspace_root: Path,
    limit: int = 1000,
) -> MergeCleanupResult:
    """Cancel active related jobs and wipe their workspaces after a merge.

    Terminal jobs (completed/failed/cancelled) are left in place but their
    workspaces are removed. The worker process itself is not stopped.
    """

    matched = jobs_for_merged_change_request(store, merged, limit=limit)
    cancelled: list[str] = []
    wiped: list[str] = []
    root = Path(workspace_root).resolve()
    for job in matched:
        if job.status in _ACTIVE:
            try:
                transition_job(
                    job,
                    JobStatus.CANCELLED,
                    error=(
                        f"Cancelled because {merged.provider} change request "
                        f"#{merged.number} was merged."
                    ),
                )
                store.save(job)
                cancelled.append(job.id)
            except JobTransitionError:
                # Race with the worker finishing the job; still wipe below.
                pass
        if _wipe_job_workspace(job, root):
            wiped.append(job.id)
            job.workspace_path = None
            store.save(job)
    return MergeCleanupResult(
        matched_job_ids=tuple(job.id for job in matched),
        cancelled_job_ids=tuple(cancelled),
        wiped_workspaces=tuple(wiped),
    )


def _job_matches_merged(job: Job, merged: MergedChangeRequest) -> bool:
    labels = job.run_spec.labels
    if labels.get("pr_id") == merged.number or labels.get("mr_iid") == merged.number:
        return True
    if labels.get("issue_id") == merged.number and labels.get("provider") == merged.provider:
        # GitHub PRs share the issue number space; prefer when head_ref also matches.
        if not merged.head_ref or labels.get("head_ref") == merged.head_ref:
            return True
    if merged.head_ref and labels.get("head_ref") == merged.head_ref:
        return True
    publication = job.publication or {}
    url = str(publication.get("change_request_url") or "")
    if merged.url and url and merged.url.rstrip("/") == url.rstrip("/"):
        return True
    if url and merged.number and f"/pull/{merged.number}" in url:
        return True
    if url and merged.number and f"/merge_requests/{merged.number}" in url:
        return True
    branch = str(publication.get("branch") or "")
    if merged.head_ref and branch == merged.head_ref:
        return True
    return False


def _wipe_job_workspace(job: Job, workspace_root: Path) -> bool:
    """Remove the job workspace directory if it exists under ``workspace_root``."""

    candidates: list[Path] = []
    if job.workspace_path:
        repo = Path(job.workspace_path).resolve()
        candidates.append(repo.parent if repo.name == "repo" else repo)
    try:
        candidates.append(JobWorkspace.under(workspace_root, job.id).root)
    except ValueError:
        pass
    wiped = False
    for path in candidates:
        resolved = path.resolve()
        if workspace_root not in resolved.parents and resolved != workspace_root:
            continue
        if resolved.exists():
            shutil.rmtree(resolved)
            wiped = True
    return wiped
