from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

from gca.executor.prune import prune_stale_run_images
from gca.executor.spec import DEFAULT_ISOLATION_IMAGE


def test_prune_stale_run_images_keeps_default_and_fresh_tags() -> None:
    now = datetime(2024, 1, 2, 12, 0, tzinfo=timezone.utc)
    stale = (now - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S %z UTC")
    fresh = (now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S %z UTC")
    calls: list[list[str]] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:  # type: ignore[type-arg]
        calls.append(list(command))
        if command[:2] == ["docker", "images"]:
            payload = "\n".join(
                [
                    f"{DEFAULT_ISOLATION_IMAGE}\tdefaultid\t{stale}",
                    f"gca/deadbeef:run\tstaleid\t{stale}",
                    f"gca/cafebabe:run\tfreshid\t{fresh}",
                ]
            )
            return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    result = prune_stale_run_images(
        older_than_seconds=3600,
        now=now,
        runner=runner,  # type: ignore[arg-type]
    )

    assert result.inspected == 3
    assert result.deleted == 1
    assert result.skipped == 2
    assert ["docker", "image", "rm", "-f", "staleid"] in calls
    assert not any(call[-1] == "defaultid" for call in calls if "rm" in call)


def test_prune_stale_run_images_noop_when_docker_missing() -> None:
    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:  # type: ignore[type-arg]
        return subprocess.CompletedProcess(command, 127, stdout="", stderr="")

    result = prune_stale_run_images(older_than_seconds=3600, runner=runner)  # type: ignore[arg-type]
    assert result.deleted == 0
    assert result.errors
