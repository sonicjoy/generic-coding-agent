from __future__ import annotations

import subprocess
from pathlib import Path

from gca.executor.docker import (
    DockerExecutor,
    looks_like_cgroup_limit_failure,
    resource_limits_disabled,
)
from gca.executor.spec import EnvironmentSpec, ImageSource


def _image(tmp_path: Path) -> ImageSource:
    return ImageSource(
        dockerfile=tmp_path / "Dockerfile",
        context=tmp_path,
        tag="gca-test:latest",
        is_default=True,
    )


def test_detects_cgroup_limit_failure_messages() -> None:
    assert looks_like_cgroup_limit_failure(
        "docker: Error response from daemon: failed to create task for container: "
        "failed to cgroupv2 ... with domain controllers -- it is in threaded mode"
    )
    assert not looks_like_cgroup_limit_failure("command not found")


def test_resource_limits_disabled_env(monkeypatch: object) -> None:
    monkeypatch.setenv("GCA_DOCKER_DISABLE_RESOURCE_LIMITS", "true")  # type: ignore[attr-defined]
    assert resource_limits_disabled()
    monkeypatch.delenv("GCA_DOCKER_DISABLE_RESOURCE_LIMITS", raising=False)  # type: ignore[attr-defined]
    assert not resource_limits_disabled()


def test_run_retries_without_limits_on_cgroup_failure(
    tmp_path: Path, monkeypatch: object
) -> None:
    monkeypatch.delenv("GCA_DOCKER_DISABLE_RESOURCE_LIMITS", raising=False)  # type: ignore[attr-defined]
    executor = DockerExecutor(
        workspace=tmp_path,
        spec=EnvironmentSpec(cpu=1, memory="256m"),
        image=_image(tmp_path),
        run_id="limits01",
        _built=True,
    )
    calls: list[list[str]] = []

    def fake_docker(
        command: list[str] | tuple[str, ...], *, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        argv = list(command)
        calls.append(argv)
        if "--cpus" in argv:
            return subprocess.CompletedProcess(
                args=argv,
                returncode=125,
                stdout="",
                stderr=(
                    "docker: Error response from daemon: failed to create task for container: "
                    "cannot enter cgroupv2 ... with domain controllers -- it is in threaded mode\n"
                ),
            )
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(executor, "_docker", fake_docker)  # type: ignore[attr-defined]
    monkeypatch.setattr(executor, "_remove_container", lambda name: None)  # type: ignore[attr-defined]
    monkeypatch.setattr(executor, "_kill_container", lambda name: None)  # type: ignore[attr-defined]

    result = executor.run(
        argv=["true"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        timeout=30,
    )

    assert result.returncode == 0
    assert "ok" in result.output
    assert len(calls) == 2
    assert "--cpus" in calls[0]
    assert "--memory" in calls[0]
    assert "--cpus" not in calls[1]
    assert "--memory" not in calls[1]


def test_run_skips_limits_when_env_disables_them(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("GCA_DOCKER_DISABLE_RESOURCE_LIMITS", "1")  # type: ignore[attr-defined]
    executor = DockerExecutor(
        workspace=tmp_path,
        spec=EnvironmentSpec(cpu=1, memory="256m"),
        image=_image(tmp_path),
        run_id="limits02",
        _built=True,
    )
    calls: list[list[str]] = []

    def fake_docker(
        command: list[str] | tuple[str, ...], *, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        argv = list(command)
        calls.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(executor, "_docker", fake_docker)  # type: ignore[attr-defined]
    monkeypatch.setattr(executor, "_remove_container", lambda name: None)  # type: ignore[attr-defined]

    result = executor.run(
        argv=["true"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        timeout=30,
    )

    assert result.returncode == 0
    assert len(calls) == 1
    assert "--cpus" not in calls[0]
