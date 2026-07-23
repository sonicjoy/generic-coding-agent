from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from gca.jobs.models import JobStatus, RepositorySpec, RunSpec
from gca.models import ModelProfile
from gca.plugins import LoadedPlugins
from gca.providers.base import ProviderError
from gca.providers.scripted import ScriptedProvider
from gca.runtime import RuntimeConfig
from gca_service.config import ServiceSettings
from gca_service.state import ServiceState
from gca_service.worker import ServiceWorker, _LeaseKeeper


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
        api_token="api-token-123456",
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


def test_worker_periodically_renews_long_running_lease(tmp_path: Path) -> None:
    settings = ServiceSettings(
        data_dir=tmp_path / "lease-service",
        api_token="api-token-123456",
        allow_local_repositories=True,
        lease_seconds=1,
    )
    state = ServiceState.build(settings)
    job = state.store.create(
        RunSpec(
            task="Long task",
            repository=RepositorySpec(str(tmp_path), ref="main"),
        )
    )
    state.queue.enqueue(job.id)
    claimed = state.queue.claim(settings.worker_id, lease_seconds=1)
    assert claimed is not None

    with _LeaseKeeper(state, claimed):
        time.sleep(1.2)
        assert state.store.requeue_expired() == 0

    assert state.store.load(job.id).status == JobStatus.RUNNING


def test_worker_run_forever_emits_job_errors(tmp_path: Path) -> None:
    settings = ServiceSettings(
        data_dir=tmp_path / "service-errors",
        api_token="api-token-123456",
        allow_local_repositories=True,
        lease_seconds=60,
        poll_seconds=0.05,
    )
    state = ServiceState.build(settings)
    source = _repository(tmp_path)
    job = state.store.create(
        RunSpec(
            task="Fix a typo",
            repository=RepositorySpec(str(source), ref="main"),
            workflow="fast",
            max_steps=1,
        ),
        max_attempts=1,
    )
    state.queue.enqueue(job.id)

    def fail_models(config: RuntimeConfig) -> LoadedPlugins:
        raise ProviderError("boom", retryable=False)

    events: list[str] = []
    stop = threading.Event()

    def on_event(message: str) -> None:
        events.append(message)
        if message.startswith(f"{job.id} failed:"):
            stop.set()

    worker = ServiceWorker(state, on_event=on_event, model_loader=fail_models)
    thread = threading.Thread(target=worker.run_forever, kwargs={"stop": stop}, daemon=True)
    thread.start()
    assert stop.wait(timeout=5), events
    stop.set()
    thread.join(timeout=2)

    assert any(event.startswith(f"{job.id} failed:") and "boom" in event for event in events)
