from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from gca.executor.docker import DockerExecutor, ensure_docker_available
from gca.executor.spec import EnvironmentSpec

docker = shutil.which("docker")


@pytest.mark.docker
@pytest.mark.skipif(docker is None, reason="docker CLI not installed")
def test_docker_default_isolation_runs_command(tmp_path: Path) -> None:
    try:
        ensure_docker_available()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"docker daemon unavailable: {exc}")

    (tmp_path / "hello.txt").write_text("hi\n", encoding="utf-8")
    executor = DockerExecutor.create(
        tmp_path,
        EnvironmentSpec(cpu=1, memory="256m", default_timeout=60),
        run_id="testdefault01",
    )
    try:
        executor.build()
        result = executor.run(
            argv=["cat", "hello.txt"],
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
            timeout=60,
        )
        assert result.returncode == 0
        assert "hi" in result.output
    finally:
        executor.cleanup(remove_image=False)
