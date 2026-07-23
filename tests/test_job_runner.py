from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

from gca.executor.fake import FakeExecutor
from gca.integrations.scm import PublicationError
from gca.jobs.models import Job, JobStatus, PublicationTarget, RepositorySpec, RunSpec
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


class _FakePublisher:
    def __init__(self) -> None:
        self.calls: list[Job] = []
        self.draft_flags: list[bool] = []

    def publish(
        self,
        job: Job,
        workspace: Path,
        repo_config: RepoConfig,
        *,
        executor: object = None,
    ) -> dict[str, object]:
        _ = workspace, repo_config, executor
        self.calls.append(job)
        target = job.run_spec.publication
        self.draft_flags.append(bool(target.draft) if target is not None else False)
        return {
            "branch": f"gca/{job.id}",
            "commit_sha": "abc123",
            "change_request_url": f"https://scm.example/changes/{job.id}",
            "no_changes": False,
        }


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
    # Completed jobs wipe the cloned repo but keep meta/session artifacts.
    assert not Path(result.workspace_path).exists()
    artifacts = Path(result.workspace_path).parent / "meta" / "artifacts" / "result.json"
    assert artifacts.is_file()
    assert '"status": "completed"' in artifacts.read_text(encoding="utf-8")
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
    assert f"POST /runs/{paused.id}/resume" in paused.last_error
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


def test_job_runner_draft_publishes_finished_implementation_on_budget_pause(
    tmp_path: Path,
) -> None:
    """Finished implementation + budget pause opens a draft CR and stays PAUSED."""

    source = _source_repository(tmp_path)
    store = SqliteJobStore(tmp_path / "jobs.sqlite3")
    queue = SqliteJobQueue(store)
    # max_steps=5 == default reserve: planning uses 1, implementation gets the
    # remaining 4 (reserve is a no-op). A 4-step implementation finishes and
    # parent pauses before review because the overall budget is exhausted, with
    # an implementation artifact present.
    created = store.create(
        RunSpec(
            task="Implement publishable work",
            repository=RepositorySpec(url=str(source), ref="main"),
            workflow="feature",
            max_steps=5,
            publication=PublicationTarget(provider="fake", base_ref="main", draft=False),
        )
    )
    queue.enqueue(created.id)
    claimed = queue.claim("worker")
    assert claimed is not None

    strong = ScriptedProvider.from_script(
        [
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {"plan": "Create work.txt then stop."},
                    }
                ]
            }
        ]
    )
    fast = ScriptedProvider.from_script(
        [
            {
                "tool_calls": [
                    {
                        "name": "create_file",
                        "arguments": {"path": "work1.txt", "content": "a"},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "create_file",
                        "arguments": {"path": "work2.txt", "content": "b"},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "create_file",
                        "arguments": {"path": "work3.txt", "content": "c"},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {"summary": "Implementation finished."},
                    }
                ]
            },
        ]
    )

    def load_models(config: object) -> LoadedPlugins:
        loaded = LoadedPlugins()
        loaded.models.register(ModelProfile("fast", fast, strength=2, speed=5, cost=1))
        loaded.models.register(ModelProfile("strong", strong, strength=5, speed=2, cost=5))
        return loaded

    publisher = _FakePublisher()
    result = JobRunner(
        store=store,
        workspace_root=tmp_path / "workspaces",
        model_loader=load_models,
        publisher=publisher,
        allow_local_repositories=True,
        executor_factory=fake_executor_factory,
    ).execute(claimed)

    assert result.status == JobStatus.PAUSED, result.last_error
    assert len(publisher.calls) == 1
    assert publisher.draft_flags == [True]
    assert result.publication.get("change_request_url") == (
        f"https://scm.example/changes/{result.id}"
    )
    assert f"POST /runs/{result.id}/resume" in result.last_error
    assert "Draft change request opened" in result.last_error
    # Operator-requested draft flag restored after draft-on-pause publish.
    assert result.run_spec.publication is not None
    assert result.run_spec.publication.draft is False


def test_job_runner_mid_implementation_pause_does_not_publish(tmp_path: Path) -> None:
    """Unfinished mid-implementation pause must not open a change request."""

    source = _source_repository(tmp_path)
    store = SqliteJobStore(tmp_path / "jobs.sqlite3")
    queue = SqliteJobQueue(store)
    # max_steps=8 with default reserve=5: planning 1, implementation capped at 2.
    # A 3-step implementation pauses before finish (no artifact).
    created = store.create(
        RunSpec(
            task="Implement mid-pause work",
            repository=RepositorySpec(url=str(source), ref="main"),
            workflow="feature",
            max_steps=8,
            publication=PublicationTarget(provider="fake", base_ref="main"),
        )
    )
    queue.enqueue(created.id)
    claimed = queue.claim("worker")
    assert claimed is not None

    strong = ScriptedProvider.from_script(
        [
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {"plan": "Create files gradually."},
                    }
                ]
            }
        ]
    )
    fast = ScriptedProvider.from_script(
        [
            {
                "tool_calls": [
                    {
                        "name": "create_file",
                        "arguments": {"path": "mid1.txt", "content": "1"},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "create_file",
                        "arguments": {"path": "mid2.txt", "content": "2"},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {"summary": "Should not finish under reserve."},
                    }
                ]
            },
        ]
    )

    def load_models(config: object) -> LoadedPlugins:
        loaded = LoadedPlugins()
        loaded.models.register(ModelProfile("fast", fast, strength=2, speed=5, cost=1))
        loaded.models.register(ModelProfile("strong", strong, strength=5, speed=2, cost=5))
        return loaded

    publisher = _FakePublisher()
    result = JobRunner(
        store=store,
        workspace_root=tmp_path / "workspaces",
        model_loader=load_models,
        publisher=publisher,
        allow_local_repositories=True,
        executor_factory=fake_executor_factory,
    ).execute(claimed)

    assert result.status == JobStatus.PAUSED, result.last_error
    assert publisher.calls == []
    assert result.publication == {}
    assert f"POST /runs/{result.id}/resume" in result.last_error


class _FailingPublisher:
    def publish(
        self,
        job: Job,
        workspace: Path,
        repo_config: RepoConfig,
        *,
        executor: object = None,
    ) -> dict[str, object]:
        _ = job, workspace, repo_config, executor
        raise PublicationError("push rejected", retryable=False)


def test_job_runner_retains_workspace_after_publish_failure(tmp_path: Path) -> None:
    source = _source_repository(tmp_path)
    store = SqliteJobStore(tmp_path / "jobs.sqlite3")
    queue = SqliteJobQueue(store)
    created = store.create(
        RunSpec(
            task="Fix a typo",
            repository=RepositorySpec(url=str(source), ref="main"),
            workflow="fast",
            max_steps=5,
            publication=PublicationTarget(provider="fake", base_ref="main"),
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

    result = JobRunner(
        store=store,
        workspace_root=tmp_path / "workspaces",
        model_loader=load_models,
        publisher=_FailingPublisher(),
        allow_local_repositories=True,
        executor_factory=fake_executor_factory,
    ).execute(claimed)

    assert result.status == JobStatus.FAILED, result.last_error
    assert result.workspace_path is not None
    repo = Path(result.workspace_path)
    assert repo.is_dir()
    assert (repo / "generated.txt").is_file()
    artifacts = repo.parent / "meta" / "artifacts" / "result.json"
    assert artifacts.is_file()
    assert '"status": "failed"' in artifacts.read_text(encoding="utf-8")
