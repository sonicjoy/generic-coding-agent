from __future__ import annotations

from pathlib import Path

from gca.jobs.lifecycle import transition_job
from gca.jobs.merge_cleanup import (
    MergedChangeRequest,
    cleanup_jobs_for_merged_change_request,
    jobs_for_merged_change_request,
)
from gca.jobs.models import JobStatus, PublicationTarget, RepositorySpec, RunSpec
from gca.jobs.queue import SqliteJobQueue
from gca.jobs.store import SqliteJobStore
from gca.workspace.layout import JobWorkspace


def test_cleanup_cancels_paused_job_and_wipes_workspace(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.sqlite3")
    queue = SqliteJobQueue(store)
    workspace_root = tmp_path / "workspaces"
    created = store.create(
        RunSpec(
            task="Address review",
            repository=RepositorySpec(url="https://github.com/owner/repo.git", ref="gca/head"),
            publication=PublicationTarget(provider="github", base_ref="main"),
            labels={"provider": "github", "pr_id": "46", "head_ref": "gca/head"},
        )
    )
    queue.enqueue(created.id)
    claimed = queue.claim("worker")
    assert claimed is not None
    transition_job(claimed, JobStatus.PAUSED, error="waiting for review")
    layout = JobWorkspace.under(workspace_root, claimed.id)
    layout.ensure_metadata()
    (layout.repository).mkdir(parents=True)
    (layout.repository / "README.md").write_text("work\n", encoding="utf-8")
    (layout.sessions / "sess.json").write_text("{}", encoding="utf-8")
    claimed.workspace_path = str(layout.repository)
    claimed.publication = {
        "branch": "gca/head",
        "change_request_url": "https://github.com/owner/repo/pull/46",
    }
    store.save(claimed)

    result = cleanup_jobs_for_merged_change_request(
        store,
        MergedChangeRequest(
            provider="github",
            project="owner/repo",
            number="46",
            url="https://github.com/owner/repo/pull/46",
            head_ref="gca/head",
        ),
        workspace_root=workspace_root,
    )

    reloaded = store.load(claimed.id)
    assert reloaded.status == JobStatus.CANCELLED
    assert "merged" in reloaded.last_error
    assert reloaded.workspace_path is None
    assert not layout.root.exists()
    assert result.matched_job_ids == (claimed.id,)
    assert result.cancelled_job_ids == (claimed.id,)
    assert result.wiped_workspaces == (claimed.id,)


def test_cleanup_wipes_completed_job_workspace_without_cancelling(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.sqlite3")
    workspace_root = tmp_path / "workspaces"
    created = store.create(
        RunSpec(
            task="Done",
            repository=RepositorySpec(url="https://github.com/owner/repo.git", ref="main"),
            labels={"pr_id": "46"},
        )
    )
    queue = SqliteJobQueue(store)
    queue.enqueue(created.id)
    claimed = queue.claim("worker")
    assert claimed is not None
    transition_job(claimed, JobStatus.COMPLETED)
    claimed.publication = {
        "change_request_url": "https://github.com/owner/repo/pull/46",
    }
    layout = JobWorkspace.under(workspace_root, claimed.id)
    layout.ensure_metadata()
    layout.repository.mkdir(parents=True)
    claimed.workspace_path = str(layout.repository)
    store.save(claimed)
    created = claimed

    result = cleanup_jobs_for_merged_change_request(
        store,
        MergedChangeRequest(
            provider="github",
            project="owner/repo",
            number="46",
            url="https://github.com/owner/repo/pull/46",
        ),
        workspace_root=workspace_root,
    )

    reloaded = store.load(created.id)
    assert reloaded.status == JobStatus.COMPLETED
    assert reloaded.workspace_path is None
    assert not layout.root.exists()
    assert result.cancelled_job_ids == ()
    assert result.wiped_workspaces == (created.id,)


def test_jobs_for_merged_change_request_matches_publication_url(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.sqlite3")
    created = store.create(
        RunSpec(
            task="Done",
            repository=RepositorySpec(url="https://github.com/owner/repo.git", ref="main"),
        )
    )
    created.status = JobStatus.COMPLETED
    created.publication = {
        "change_request_url": "https://github.com/owner/repo/pull/99",
    }
    store.save(created)
    other = store.create(
        RunSpec(
            task="Other",
            repository=RepositorySpec(url="https://github.com/owner/repo.git", ref="main"),
            labels={"pr_id": "98"},
        )
    )
    store.save(other)

    matched = jobs_for_merged_change_request(
        store,
        MergedChangeRequest(
            provider="github",
            project="owner/repo",
            number="99",
            url="https://github.com/owner/repo/pull/99",
        ),
    )
    assert [job.id for job in matched] == [created.id]
