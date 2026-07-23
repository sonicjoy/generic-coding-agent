"""Docker-backed command executor with resource limits and cleanup."""

from __future__ import annotations

import os
import shlex
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from gca.executor.protocol import CommandResult
from gca.executor.spec import EnvironmentSpec, ImageSource, resolve_image_source

_DOCKER_MISSING = (
    "Docker Engine is required to run GCA. Install Docker and ensure the daemon "
    "is reachable (`docker info`), then retry."
)


class DockerExecutorError(RuntimeError):
    """Raised when Docker preflight, build, or run fails."""


def ensure_docker_available(*, timeout: int = 30) -> None:
    """Fail fast when the Docker CLI or daemon is unavailable."""

    try:
        result = subprocess.run(
            ["docker", "info"],
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DockerExecutorError(_DOCKER_MISSING) from exc
    except subprocess.TimeoutExpired as exc:
        raise DockerExecutorError("timed out while checking Docker availability") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        message = _DOCKER_MISSING
        if detail:
            message = f"{message}\n{detail}"
        raise DockerExecutorError(message)


@dataclass
class DockerExecutor:
    """Build and run target-repo commands inside an isolation container."""

    workspace: Path
    spec: EnvironmentSpec
    image: ImageSource
    run_id: str
    _built: bool = False
    _active_containers: set[str] = field(default_factory=set)

    @classmethod
    def create(
        cls,
        workspace: Path,
        spec: EnvironmentSpec | None = None,
        *,
        run_id: str | None = None,
    ) -> DockerExecutor:
        """Construct an executor and resolve the image source for ``workspace``."""

        resolved_spec = spec or EnvironmentSpec()
        identity = run_id or uuid.uuid4().hex
        image = resolve_image_source(workspace, resolved_spec, run_id=identity)
        return cls(
            workspace=Path(workspace).resolve(),
            spec=resolved_spec,
            image=image,
            run_id=identity,
        )

    def build(self, *, timeout: int = 600) -> None:
        """Build the isolation image when it is not already available."""

        ensure_docker_available()
        if self.image.is_default and self._image_exists(self.image.tag):
            self._built = True
            return
        command = [
            "docker",
            "build",
            "-f",
            str(self.image.dockerfile),
            "-t",
            self.image.tag,
            str(self.image.context),
        ]
        result = self._docker(command, timeout=timeout)
        if result.returncode != 0:
            output = ((result.stdout or "") + (result.stderr or "")).strip()
            raise DockerExecutorError(f"docker build failed for {self.image.tag}:\n{output}")
        self._built = True

    def run(
        self,
        *,
        argv: list[str] | None = None,
        shell_command: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str],
        timeout: int,
    ) -> CommandResult:
        """Run a command in a named container with CPU/memory limits."""

        if not self._built:
            self.build()
        if (argv is None) == (shell_command is None):
            raise ValueError("provide exactly one of argv or shell_command")

        container = f"gca-run-{self.run_id}-{uuid.uuid4().hex[:8]}"
        workdir = self._container_cwd(cwd)
        command = [
            "docker",
            "run",
            "--name",
            container,
            "--rm",
            "--cpus",
            str(self.spec.cpu),
            "--memory",
            self.spec.memory,
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--network",
            "bridge" if self.spec.network else "none",
            "--workdir",
            workdir,
            "--mount",
            f"type=bind,source={self.workspace},target={self.spec.working_dir}",
        ]
        for key, value in env.items():
            command.extend(["--env", f"{key}={value}"])
        command.append(self.image.tag)
        if shell_command is not None:
            command.extend(["bash", "-lc", shell_command])
        else:
            assert argv is not None
            command.extend(argv)

        self._active_containers.add(container)
        try:
            result = self._docker(command, timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill_container(container)
            rendered = shell_command if shell_command is not None else shlex.join(argv or [])
            return CommandResult(
                returncode=124,
                output=f"command timed out after {timeout}s: {rendered}",
                timed_out=True,
            )
        finally:
            self._active_containers.discard(container)
            self._remove_container(container)

        output = (result.stdout or "") + (result.stderr or "")
        return CommandResult(returncode=result.returncode, output=output, timed_out=False)

    def cleanup(self, *, remove_image: bool = False) -> None:
        """Remove any leftover containers and optionally the per-run image."""

        for container in list(self._active_containers):
            self._kill_container(container)
            self._remove_container(container)
            self._active_containers.discard(container)
        should_remove = remove_image or (
            self.spec.remove_image_after_run and not self.image.is_default
        )
        if should_remove and not self.image.is_default:
            self._docker(["docker", "image", "rm", "-f", self.image.tag], timeout=60)

    def _container_cwd(self, cwd: Path | None) -> str:
        root = self.workspace.resolve()
        target = root if cwd is None else Path(cwd).resolve()
        if target != root and root not in target.parents:
            raise DockerExecutorError(f"command cwd escapes workspace: {cwd}")
        relative = Path() if target == root else target.relative_to(root)
        parts = (self.spec.working_dir.rstrip("/"), *relative.parts)
        return "/" + "/".join(part for part in parts if part not in {"", "/"})

    def _image_exists(self, tag: str) -> bool:
        result = self._docker(["docker", "image", "inspect", tag], timeout=30)
        return result.returncode == 0

    def _kill_container(self, name: str) -> None:
        self._docker(["docker", "kill", name], timeout=30)

    def _remove_container(self, name: str) -> None:
        self._docker(["docker", "rm", "-f", name], timeout=30)

    def _docker(
        self,
        command: Sequence[str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                list(command),
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise DockerExecutorError(_DOCKER_MISSING) from exc
