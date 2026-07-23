from __future__ import annotations

from pathlib import Path

import pytest

from gca.executor.fake import FakeExecutor
from gca.executor.lifecycle import RunLifecycle, should_wipe_workspace
from gca.executor.spec import (
    DEFAULT_ISOLATION_IMAGE,
    EnvironmentSpec,
    EnvironmentSpecError,
    resolve_image_source,
)
from gca.repo_config import load_repo_config
from gca.tools.base import ToolContext
from gca.tools.shell import RunCommandTool


def test_environment_spec_defaults() -> None:
    spec = EnvironmentSpec.from_mapping({})
    assert spec.dockerfile is None
    assert spec.working_dir == "/workspace"
    assert spec.network is False


def test_environment_spec_rejects_absolute_dockerfile() -> None:
    with pytest.raises(EnvironmentSpecError, match="relative"):
        EnvironmentSpec.from_mapping({"dockerfile": "/tmp/Dockerfile"})


def test_resolve_default_image_when_no_dockerfile(tmp_path: Path) -> None:
    source = resolve_image_source(tmp_path, EnvironmentSpec(), run_id="abc123")
    assert source.is_default
    assert source.tag == DEFAULT_ISOLATION_IMAGE
    assert source.dockerfile.name == "default.Dockerfile"


def test_resolve_repo_dockerfile_agent(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile.agent"
    dockerfile.write_text("FROM debian:bookworm-slim\n", encoding="utf-8")
    source = resolve_image_source(tmp_path, EnvironmentSpec(), run_id="deadbeef")
    assert not source.is_default
    assert source.dockerfile == dockerfile.resolve()
    assert source.tag.startswith("gca/deadbeef")


def test_load_environment_from_manifest(tmp_path: Path) -> None:
    config_dir = tmp_path / ".gca"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        """
version: 1
environment:
  cpu: 1.5
  memory: 2g
  network: true
""",
        encoding="utf-8",
    )
    config = load_repo_config(tmp_path, [config_dir / "config.yaml"])
    assert config.environment.cpu == 1.5
    assert config.environment.memory == "2g"
    assert config.environment.network is True


def test_agent_config_yaml_merges_environment(tmp_path: Path) -> None:
    gca = tmp_path / ".gca"
    gca.mkdir()
    (gca / "config.yaml").write_text("version: 1\n", encoding="utf-8")
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "config.yaml").write_text(
        "environment:\n  memory: 8g\n",
        encoding="utf-8",
    )
    config = load_repo_config(tmp_path)
    assert config.environment.memory == "8g"


def test_run_command_uses_executor(tmp_path: Path) -> None:
    executor = FakeExecutor()
    result = RunCommandTool().run(
        ToolContext(workspace=tmp_path, executor=executor),
        command="echo hi",
    )
    assert result.ok
    assert executor.calls
    assert executor.calls[0].shell_command == "echo hi"


def test_lifecycle_sync_back_and_conditional_wipe(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "keep.txt").write_text("old\n", encoding="utf-8")
    config = load_repo_config(source, [])
    lifecycle = RunLifecycle.for_local_run(
        source,
        tmp_path / "runs",
        config,
        run_id="aa" * 16,
        executor=FakeExecutor(),
    )
    (lifecycle.workspace / "keep.txt").write_text("new\n", encoding="utf-8")
    (lifecycle.workspace / "added.txt").write_text("x\n", encoding="utf-8")
    synced = lifecycle.sync_back()
    assert "keep.txt" in synced.changed_files
    assert "added.txt" in synced.changed_files
    assert (source / "keep.txt").read_text(encoding="utf-8") == "new\n"
    assert should_wipe_workspace("completed")
    assert not should_wipe_workspace("paused")
    lifecycle.cleanup(wipe_workspace=True)
    assert not lifecycle.workspace.exists()
    assert executor_cleaned(lifecycle)


def executor_cleaned(lifecycle: RunLifecycle) -> bool:
    executor = lifecycle.executor
    assert isinstance(executor, FakeExecutor)
    return executor.cleaned_up
