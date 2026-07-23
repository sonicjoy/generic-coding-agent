"""Prune stale per-run isolation images left behind after agent jobs."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from gca.executor.spec import DEFAULT_ISOLATION_IMAGE

_RUN_TAG = re.compile(r"^gca/[0-9a-f-]+:run$")
DockerRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class PruneResult:
    """Outcome of one image prune pass."""

    inspected: int = 0
    deleted: int = 0
    skipped: int = 0
    errors: tuple[str, ...] = ()


def prune_stale_run_images(
    *,
    older_than_seconds: int,
    now: datetime | None = None,
    runner: DockerRunner | None = None,
) -> PruneResult:
    """Delete ``gca/<run-id>:run`` images older than the retention window.

    The shared default isolation image is never removed. Missing Docker is treated
    as a no-op so janitors can run on hosts without a daemon during tests.
    """

    if older_than_seconds < 0:
        return PruneResult()
    execute = runner or _docker
    listed = execute(
        [
            "docker",
            "images",
            "--filter",
            "reference=gca/*",
            "--format",
            "{{.Repository}}:{{.Tag}}\t{{.ID}}\t{{.CreatedAt}}",
        ]
    )
    if listed.returncode != 0:
        detail = (listed.stderr or listed.stdout or "docker images failed").strip()
        return PruneResult(errors=(detail,))

    clock = now or datetime.now(timezone.utc)
    deleted = 0
    skipped = 0
    inspected = 0
    errors: list[str] = []
    for raw_line in listed.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            skipped += 1
            continue
        reference, image_id, created_at = parts[0], parts[1], parts[2]
        inspected += 1
        if reference == DEFAULT_ISOLATION_IMAGE or not _RUN_TAG.match(reference):
            skipped += 1
            continue
        created = _parse_docker_created(created_at)
        if created is None:
            skipped += 1
            continue
        age = (clock - created).total_seconds()
        if age < older_than_seconds:
            skipped += 1
            continue
        removed = execute(["docker", "image", "rm", "-f", image_id])
        if removed.returncode != 0:
            detail = (removed.stderr or removed.stdout or f"failed to remove {reference}").strip()
            errors.append(detail)
            continue
        deleted += 1
    return PruneResult(
        inspected=inspected,
        deleted=deleted,
        skipped=skipped,
        errors=tuple(errors),
    )


def _docker(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            shell=False,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(list(command), returncode=127, stdout="", stderr="")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            list(command),
            returncode=124,
            stdout="",
            stderr="docker command timed out",
        )


def _parse_docker_created(value: str) -> datetime | None:
    """Parse Docker's CreatedAt formats into an aware UTC datetime."""

    text = value.strip()
    # Example: 2024-01-02 03:04:05 +0000 UTC
    for fmt in (
        "%Y-%m-%d %H:%M:%S %z %Z",
        "%Y-%m-%d %H:%M:%S %z",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
