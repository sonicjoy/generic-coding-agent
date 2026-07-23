"""Environment specification for containerized agent command execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_ISOLATION_IMAGE = "gca/default-isolation:latest"
DEFAULT_WORKING_DIR = "/workspace"
DEFAULT_CPU = 2.0
DEFAULT_MEMORY = "4g"
DEFAULT_TIMEOUT = 300


class EnvironmentSpecError(ValueError):
    """Raised when environment configuration is invalid."""


@dataclass(frozen=True)
class EnvironmentSpec:
    """Limits and image settings for one isolated run."""

    dockerfile: str | None = None
    working_dir: str = DEFAULT_WORKING_DIR
    cpu: float = DEFAULT_CPU
    memory: str = DEFAULT_MEMORY
    network: bool = False
    default_timeout: int = DEFAULT_TIMEOUT
    remove_image_after_run: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> EnvironmentSpec:
        """Parse an ``environment:`` mapping from repository config."""

        data = dict(raw or {})
        unknown = sorted(
            set(data)
            - {
                "dockerfile",
                "working_dir",
                "cpu",
                "memory",
                "network",
                "default_timeout",
                "remove_image_after_run",
            }
        )
        if unknown:
            raise EnvironmentSpecError(f"unknown environment keys: {', '.join(unknown)}")
        dockerfile = data.get("dockerfile")
        if dockerfile is not None:
            if not isinstance(dockerfile, str) or not dockerfile.strip():
                raise EnvironmentSpecError("environment.dockerfile must be a non-empty string")
            dockerfile = dockerfile.strip()
            if Path(dockerfile).is_absolute() or ".." in Path(dockerfile).parts:
                raise EnvironmentSpecError(
                    "environment.dockerfile must be a relative path without '..'"
                )
        working_dir = data.get("working_dir", DEFAULT_WORKING_DIR)
        if not isinstance(working_dir, str) or not working_dir.startswith("/"):
            raise EnvironmentSpecError("environment.working_dir must be an absolute container path")
        cpu = data.get("cpu", DEFAULT_CPU)
        if isinstance(cpu, bool) or not isinstance(cpu, (int, float)) or float(cpu) <= 0:
            raise EnvironmentSpecError("environment.cpu must be a positive number")
        memory = data.get("memory", DEFAULT_MEMORY)
        if not isinstance(memory, str) or not memory.strip():
            raise EnvironmentSpecError("environment.memory must be a non-empty string")
        network = data.get("network", False)
        if not isinstance(network, bool):
            raise EnvironmentSpecError("environment.network must be a boolean")
        timeout = data.get("default_timeout", DEFAULT_TIMEOUT)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 3600:
            raise EnvironmentSpecError("environment.default_timeout must be an integer 1..3600")
        remove_image = data.get("remove_image_after_run", False)
        if not isinstance(remove_image, bool):
            raise EnvironmentSpecError("environment.remove_image_after_run must be a boolean")
        return cls(
            dockerfile=dockerfile,
            working_dir=working_dir.rstrip("/") or "/",
            cpu=float(cpu),
            memory=memory.strip(),
            network=network,
            default_timeout=timeout,
            remove_image_after_run=remove_image,
        )


@dataclass(frozen=True)
class ImageSource:
    """Resolved Dockerfile used to build the isolation image."""

    dockerfile: Path
    context: Path
    tag: str
    is_default: bool


def default_dockerfile_path() -> Path:
    """Return the packaged default isolation Dockerfile path."""

    path = Path(__file__).resolve().parent / "default.Dockerfile"
    if not path.is_file():
        raise EnvironmentSpecError(f"packaged default isolation Dockerfile missing: {path}")
    return path


def resolve_image_source(
    workspace: Path,
    spec: EnvironmentSpec,
    *,
    run_id: str,
) -> ImageSource:
    """Prefer a repo Dockerfile.agent / configured path; else the packaged default."""

    root = Path(workspace).resolve()
    configured = spec.dockerfile
    candidates: list[Path] = []
    if configured:
        candidates.append((root / configured).resolve())
    else:
        candidates.append((root / "Dockerfile.agent").resolve())

    for dockerfile in candidates:
        if configured is None and dockerfile.name != "Dockerfile.agent":
            continue
        if not dockerfile.is_file():
            if configured is not None:
                raise EnvironmentSpecError(f"environment dockerfile not found: {dockerfile}")
            continue
        if root not in dockerfile.parents and dockerfile.parent != root:
            raise EnvironmentSpecError(f"environment dockerfile escapes workspace: {dockerfile}")
        return ImageSource(
            dockerfile=dockerfile,
            context=dockerfile.parent,
            tag=f"gca/{_safe_tag(run_id)}:run",
            is_default=False,
        )

    default_file = default_dockerfile_path()
    return ImageSource(
        dockerfile=default_file,
        context=default_file.parent,
        tag=DEFAULT_ISOLATION_IMAGE,
        is_default=True,
    )


def _safe_tag(run_id: str) -> str:
    cleaned = "".join(character if character.isalnum() else "-" for character in run_id.lower())
    cleaned = cleaned.strip("-") or "run"
    return cleaned[:120]
