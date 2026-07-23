"""Persist lightweight job evidence outside the wiped repository tree."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from gca.jobs.models import Job
from gca.workspace.layout import JobWorkspace


def persist_job_artifacts(layout: JobWorkspace, job: Job, repository: Path) -> Path:
    """Write result summary and git diff under ``meta/artifacts`` before wipe."""

    layout.ensure_metadata()
    artifacts = layout.metadata / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    payload = {
        "job_id": job.id,
        "status": job.status.value,
        "session_id": job.session_id,
        "result_summary": job.result_summary,
        "last_error": job.last_error,
        "publication": job.publication,
        "labels": dict(job.run_spec.labels),
        "updated_at": job.updated_at,
    }
    (artifacts / "result.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    diff = _git_diff(repository)
    if diff is not None:
        (artifacts / "diff.patch").write_text(diff, encoding="utf-8")
    return artifacts


def _git_diff(repository: Path) -> str | None:
    if not (repository / ".git").exists():
        return None
    try:
        completed = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout
