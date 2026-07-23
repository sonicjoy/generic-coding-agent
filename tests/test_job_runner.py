from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

from gca.executor.fake import FakeExecutor
from gca.jobs.models import JobStatus, RepositorySpec, RunSpec
from gca.jobs.queue import SqliteJobQueue
from gca.jobs.runner import JobRunner
from gca.jobs.store import SqliteJobStore
from gca.models import ModelProfile
from gca.plugins import LoadedPlugins
from gca.providers.base import ProviderError
from gca.providers.scripted import ScriptedProvider
from gca.repo_config import RepoConfig


def fake_executor_factory(
    workspace: Path,
    repo_config: RepoConfig,
    run_id: str,
) -> FakeExecutor:
    _ = workspace, repo_config, run_id
    return FakeExecutor(execute_locally=True)


def _source_repository(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=source, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.test"],
        cwd=source,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=source, check=True)
    (source / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=source, check=True, capture_output=True)
    return source


def test_job_runner_clones_runs_and_persists_session(tmp_path: Path) -> None:
    source = _source_repository(tmp_path)
    store = SqliteJobStore(tmp_path / "jobs.sqlite3")
    queue = SqliteJobQueue(store)
    spec = RunSpec(
        task="Fix a typo",
        repository=RepositorySpec(url=str(source), ref="main"),
        workflow="fast",
        max_steps=5,
    )
    created = store.create(spec)
    queue.enqueue(created.id)
    claimed = queue.claim("worker")
    assert claimed is not None
    provider = ScriptedProvider.from_script(
        [
            {
                "tool_calls": [
                    {
                        "name": "create_file",
                        "arguments": {"path": "generated.txt", "content": "done\n"},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {"summary": "Generated the requested file."},
                    }
                ]
            },
        ]
    )

    def load_models(config: object) -> LoadedPlugins:
        loaded = LoadedPlugins()
        loaded.models.register(ModelProfile("scripted", provider, speed=5, cost=1))
        return loaded

    runner = JobRunner(
        store=store,
        workspace_root=tmp_path / "workspaces",
        model_loader=load_models,
        allow_local_repositories=True,
        executor_factory=fake_executor_factory,
    )

    result = runner.execute(claimed)

    assert result.status == JobStatus.COMPLETED, result.last_error
    assert result.session_id is not None
    assert result.workspace_path is not None
    # Terminal jobs wipe the ephemeral workspace after cleanup.
    assert not Path(result.workspace_path).exists()
    assert store.load(result.id).status == JobStatus.COMPLETED


def test_job_runner_resumes_paused_session(tmp_path: Path) -> None:
    source = _source_repository(tmp_path)
    store = SqliteJobStore(tmp_path / "jobs.sqlite3")
    queue = SqliteJobQueue(store)
    created = store.create(
        RunSpec(
            task="Fix a typo",
            repository=RepositorySpec(url=str(source), ref="main"),
            workflow="fast",
            max_steps=1,
        )
    )
    queue.enqueue(created.id)
    claimed = queue.claim("worker")
    assert claimed is not None
    provider = ScriptedProvider.from_script(
        [
            {
                "tool_calls": [
                    {
                        "name": "create_file",
                        "arguments": {"path": "paused.txt", "content": "resumable\n"},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {"summary": "Resume completed."},
                    }
                ]
            },
        ]
    )

    def load_models(config: object) -> LoadedPlugins:
        loaded = LoadedPlugins()
        loaded.models.register(ModelProfile("scripted", provider, speed=5, cost=1))
        return loaded

    runner = JobRunner(
        store=store,
        workspace_root=tmp_path / "workspaces",
        model_loader=load_models,
        allow_local_repositories=True,
        executor_factory=fake_executor_factory,
    )
    paused = runner.execute(claimed)
    assert paused.status == JobStatus.PAUSED, paused.last_error
    paused.run_spec = replace(paused.run_spec, max_steps=2)
    store.save(paused)
    queue.enqueue(paused.id)
    resumed = queue.claim("worker")
    assert resumed is not None

    completed = runner.execute(resumed)

    assert completed.status == JobStatus.COMPLETED, completed.last_error
    assert completed.session_id == paused.session_id


def test_job_runner_requeues_retryable_provider_failure(tmp_path: Path) -> None:
    source = _source_repository(tmp_path)
    store = SqliteJobStore(tmp_path / "jobs.sqlite3")
    queue = SqliteJobQueue(store)
    created = store.create(
        RunSpec(
            task="Fix a typo",
            repository=RepositorySpec(url=str(source), ref="main"),
            workflow="fast",
        ),
        max_attempts=2,
    )
    queue.enqueue(created.id)
    claimed = queue.claim("worker")
    assert claimed is not None

    def fail_models(config: object) -> LoadedPlugins:
        raise ProviderError("rate limited", retryable=True)

    events: list[str] = []
    result = JobRunner(
        store=store,
        workspace_root=tmp_path / "workspaces",
        model_loader=fail_models,
        allow_local_repositories=True,
        executor_factory=fake_executor_factory,
        on_event=events.append,
    ).execute(claimed)

    assert result.status == JobStatus.QUEUED
    assert "rate limited" in result.last_error
    assert result.not_before > 0
    assert any(
        event.startswith(f"[job] {result.id} queued:") and "rate limited" in event
        for event in events
    )


def test_job_runner_emits_paused_status_to_on_event(tmp_path: Path) -> None:
    source = _source_repository(tmp_path)
    store = SqliteJobStore(tmp_path / "jobs.sqlite3")
    queue = SqliteJobQueue(store)
    created = store.create(
        RunSpec(
            task="Fix a typo",
            repository=RepositorySpec(url=str(source), ref="main"),
            workflow="fast",
            max_steps=1,
        )
    )
    queue.enqueue(created.id)
    claimed = queue.claim("worker")
    assert claimed is not None
    provider = ScriptedProvider.from_script(
        [
            {
                "tool_calls": [
                    {
                        "name": "create_file",
                        "arguments": {"path": "paused.txt", "content": "resumable\n"},
                    }
                ]
            },
        ]
    )

    def load_models(config: object) -> LoadedPlugins:
        loaded = LoadedPlugins()
        loaded.models.register(ModelProfile("scripted", provider, speed=5, cost=1))
        return loaded

    events: list[str] = []
    paused = JobRunner(
        store=store,
        workspace_root=tmp_path / "workspaces",
        model_loader=load_models,
        allow_local_repositories=True,
        executor_factory=fake_executor_factory,
        on_event=events.append,
    ).execute(claimed)

    assert paused.status == JobStatus.PAUSED
    assert any(
        event.startswith(f"[job] {paused.id} paused:") and "Step budget" in event
        for event in events
    )


def test_hosted_job_rejects_unapproved_repository_tool_secrets(tmp_path: Path) -> None:
    source = _source_repository(tmp_path)
    config_dir = source / ".gca"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        """
version: 1
tools:
  secret_access:
    run_tests: [DATABASE_URL]
  fixed_commands:
    run_tests:
      argv: [python, --version]
""",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", ".gca/config.yaml"], cwd=source, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Add agent config"],
        cwd=source,
        check=True,
        capture_output=True,
    )
    store = SqliteJobStore(tmp_path / "jobs.sqlite3")
    queue = SqliteJobQueue(store)
    created = store.create(
        RunSpec(
            task="Fix a typo",
            repository=RepositorySpec(str(source), ref="main"),
            workflow="fast",
        )
    )
    queue.enqueue(created.id)
    claimed = queue.claim("worker")
    assert claimed is not None

    events: list[str] = []
    result = JobRunner(
        store=store,
        workspace_root=tmp_path / "workspaces",
        model_loader=lambda config: LoadedPlugins(),
        allow_local_repositories=True,
        executor_factory=fake_executor_factory,
        on_event=events.append,
    ).execute(claimed)

    assert result.status == JobStatus.FAILED
    assert "unapproved tool secret grants" in result.last_error
    assert any(
        event.startswith(f"[job] {result.id} failed:") and "unapproved tool secret grants" in event
        for event in events
    )
