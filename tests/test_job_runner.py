from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

from gca.jobs.models import JobStatus, RepositorySpec, RunSpec
from gca.jobs.queue import SqliteJobQueue
from gca.jobs.runner import JobRunner
from gca.jobs.store import SqliteJobStore
from gca.models import ModelProfile
from gca.plugins import LoadedPlugins
from gca.providers.scripted import ScriptedProvider


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
    )

    result = runner.execute(claimed)

    assert result.status == JobStatus.COMPLETED, result.last_error
    assert result.session_id is not None
    assert result.workspace_path is not None
    assert (Path(result.workspace_path) / "generated.txt").read_text(encoding="utf-8") == "done\n"
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
