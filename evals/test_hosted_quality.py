"""Offline hosted-runner quality gates."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support.git_repo import run_git as _git
from tests.support.scm import FakeScmAdapter

from gca.integrations.scm import ChangeRequest, PublicationController
from gca.jobs.models import JobStatus, PublicationTarget, RepositorySpec, RunSpec
from gca.jobs.queue import SqliteJobQueue
from gca.jobs.runner import JobRunner
from gca.jobs.store import SqliteJobStore
from gca.models import ModelProfile
from gca.plugins import LoadedPlugins
from gca.providers.scripted import ScriptedProvider
from gca.runtime import RuntimeConfig


class RecordingAdapter(FakeScmAdapter):
    """Like FakeScmAdapter but tracks pushes as ``branches`` and never links issues."""

    def __init__(self) -> None:
        super().__init__()
        self.branches = self.pushed

    def link_branch_to_issue(
        self,
        repository_url: str,
        branch: str,
        issue_id: str,
        oid: str,
    ) -> bool:
        _ = repository_url, branch, issue_id, oid
        return False

    def open_change_request(self, request: ChangeRequest) -> str:
        self.requests.append(request)
        return "https://scm.example/changes/1"


@pytest.mark.eval
def test_hosted_job_clones_runs_reviews_and_publishes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.email", "tests@example.test")
    _git(source, "config", "user.name", "Tests")
    config_dir = source / ".gca"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        """
version: 1
publication:
  allowed_paths: [generated.txt]
  max_files: 1
  max_changed_lines: 5
""",
        encoding="utf-8",
    )
    (source / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "Initial")

    store = SqliteJobStore(tmp_path / "jobs.sqlite3")
    queue = SqliteJobQueue(store)
    job = store.create(
        RunSpec(
            task="Fix a typo",
            repository=RepositorySpec(str(source), ref="main"),
            workflow="fast",
            max_steps=5,
            publication=PublicationTarget(provider="fake", base_ref="main"),
        )
    )
    queue.enqueue(job.id)
    claimed = queue.claim("eval-worker")
    assert claimed is not None
    provider = ScriptedProvider.from_script(
        [
            {
                "tool_calls": [
                    {
                        "name": "create_file",
                        "arguments": {"path": "generated.txt", "content": "verified\n"},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {"summary": "Implementation verified."},
                    }
                ]
            },
        ]
    )

    def load_models(config: RuntimeConfig) -> LoadedPlugins:
        loaded = LoadedPlugins()
        loaded.models.register(ModelProfile("scripted", provider, speed=5, cost=1))
        return loaded

    adapter = RecordingAdapter()
    runner = JobRunner(
        store=store,
        workspace_root=tmp_path / "workspaces",
        model_loader=load_models,
        publisher=PublicationController({"fake": adapter}),
        allow_local_repositories=True,
    )

    result = runner.execute(claimed)

    assert result.status == JobStatus.COMPLETED, result.last_error
    assert result.publication["change_request_url"] == "https://scm.example/changes/1"
    assert adapter.branches == [f"gca/{job.id[:12]}"]
    assert len(adapter.requests) == 1
