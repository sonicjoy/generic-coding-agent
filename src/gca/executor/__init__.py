"""Containerized command execution for isolated agent workspaces."""

from gca.executor.docker import DockerExecutor, DockerExecutorError, ensure_docker_available
from gca.executor.fake import FakeExecutor
from gca.executor.lifecycle import RunLifecycle, SyncResult
from gca.executor.protocol import CommandExecutor, CommandResult
from gca.executor.prune import PruneResult, prune_stale_run_images
from gca.executor.spec import (
    DEFAULT_ISOLATION_IMAGE,
    EnvironmentSpec,
    ImageSource,
    resolve_image_source,
)

__all__ = [
    "CommandExecutor",
    "CommandResult",
    "DEFAULT_ISOLATION_IMAGE",
    "DockerExecutor",
    "DockerExecutorError",
    "EnvironmentSpec",
    "FakeExecutor",
    "ImageSource",
    "PruneResult",
    "RunLifecycle",
    "SyncResult",
    "ensure_docker_available",
    "prune_stale_run_images",
    "resolve_image_source",
]
