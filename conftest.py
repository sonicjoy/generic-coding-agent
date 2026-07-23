"""Root pytest configuration — applies to tests/ and evals/."""

from __future__ import annotations

import pytest

from gca.executor.fake import FakeExecutor


@pytest.fixture(autouse=True)
def _use_fake_executor_without_docker(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
):
    """Route DockerExecutor.create to FakeExecutor unless the test is marked docker."""

    if request.node.get_closest_marker("docker") is not None:
        return

    def _create(workspace, spec=None, *, run_id=None):  # noqa: ANN001
        _ = workspace, spec, run_id
        return FakeExecutor(execute_locally=True)

    monkeypatch.setattr(
        "gca.executor.docker.DockerExecutor.create",
        staticmethod(_create),
    )
    monkeypatch.setattr(
        "gca.executor.lifecycle.DockerExecutor.create",
        staticmethod(_create),
    )
