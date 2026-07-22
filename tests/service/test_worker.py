from __future__ import annotations

import subprocess
from pathlib import Path

from gca.jobs.models import JobStatus, RepositorySpec, RunSpec
from gca.models import ModelProfile
from gca.plugins import LoadedPlugins
from gca.providers.scripted import ScriptedProvider
from gca.runtime import RuntimeConfig
from gca_service.config import ServiceSettings
from gca_service.state import ServiceState
from gca_service.worker import ServiceWorker


def _repository(tmp_path: Path) -> Path:
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


def test_worker_claims_and_completes_scripted_job(tmp_path: Path) -> None:
    settings = ServiceSettings(
        data_dir=tmp_path / "service",
        api_token="token",
        allow_local_repositories=True,
        lease_seconds=60,
    )
    state = ServiceState.build(settings)
    source = _repository(tmp_path)
    job = state.store.create(
        RunSpec(
            task="Fix a typo",
            repository=RepositorySpec(str(source), ref="main"),
            workflow="fast",
            max_steps=3,
        )
    )
    state.queue.enqueue(job.id)
    provider = ScriptedProvider.from_script(
        [
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {"summary": "Worker completed."},
                    }
                ]
            }
        ]
    )

    def load_models(config: RuntimeConfig) -> LoadedPlugins:
        loaded = LoadedPlugins()
        loaded.models.register(ModelProfile("scripted", provider, speed=5, cost=1))
        return loaded

    result = ServiceWorker(state, model_loader=load_models).run_once()

    assert result is not None
    assert result.status == JobStatus.COMPLETED, result.last_error
    assert state.store.load(job.id).status == JobStatus.COMPLETED
