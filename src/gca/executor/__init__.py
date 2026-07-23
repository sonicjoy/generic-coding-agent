"""Containerized command execution for isolated agent workspaces."""

from gca.executor.docker import DockerExecutor, DockerExecutorError, ensure_docker_available
from gca.executor.fake import FakeExecutor
from gca.executor.lifecycle import RunLifecycle, SyncResult
from gca.executor.protocol import CommandExecutor, CommandResult
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
    "RunLifecycle",
    "SyncResult",
    "ensure_docker_available",
    "resolve_image_source",
]
