"""Ephemeral workspace allocation, sync-back, and status-conditional teardown."""

from __future__ import annotations

import hashlib
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from gca.executor.docker import DockerExecutor
from gca.executor.protocol import CommandExecutor
from gca.repo_config import RepoConfig
from gca.workspace.layout import JobWorkspace, normalize_run_id

_COPY_IGNORE = shutil.ignore_patterns(
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "*.pyc",
)


@dataclass(frozen=True)
class SyncResult:
    """Files copied from an ephemeral workspace back to the source tree."""

    changed_files: tuple[str, ...] = ()


@dataclass
class RunLifecycle:
    """Owns per-run workspace materialization, executor, and cleanup policy."""

    run_id: str
    workspace: Path
    executor: CommandExecutor
    source_workspace: Path | None = None
    baseline_hashes: dict[str, str] = field(default_factory=dict)
    _cleaned: bool = False

    @classmethod
    def for_repository(
        cls,
        repository: Path,
        repo_config: RepoConfig,
        *,
        run_id: str | None = None,
        executor: CommandExecutor | None = None,
    ) -> RunLifecycle:
        """Attach an executor to an already-materialized repository workspace."""

        identity = normalize_run_id(run_id or uuid.uuid4().hex)
        command_executor = executor or DockerExecutor.create(
            repository,
            repo_config.environment,
            run_id=identity,
        )
        if isinstance(command_executor, DockerExecutor):
            command_executor.build()
        return cls(run_id=identity, workspace=Path(repository).resolve(), executor=command_executor)

    @classmethod
    def for_local_run(
        cls,
        source: Path,
        runs_root: Path,
        repo_config: RepoConfig,
        *,
        run_id: str | None = None,
        executor: CommandExecutor | None = None,
    ) -> RunLifecycle:
        """Copy ``source`` into an ephemeral run workspace and build an executor."""

        identity = normalize_run_id(run_id or uuid.uuid4().hex)
        layout = JobWorkspace.under(runs_root, identity)
        layout.ensure_metadata()
        destination = layout.repository
        if destination.exists():
            shutil.rmtree(destination)
        _copy_workspace(Path(source).resolve(), destination)
        baseline = _hash_tree(destination)
        command_executor = executor or DockerExecutor.create(
            destination,
            repo_config.environment,
            run_id=identity,
        )
        if isinstance(command_executor, DockerExecutor):
            command_executor.build()
        return cls(
            run_id=identity,
            workspace=destination,
            executor=command_executor,
            source_workspace=Path(source).resolve(),
            baseline_hashes=baseline,
        )

    def sync_back(self) -> SyncResult:
        """Copy changed files from the ephemeral workspace to the source tree."""

        if self.source_workspace is None:
            return SyncResult()
        current = _hash_tree(self.workspace)
        changed: list[str] = []
        for relative, digest in current.items():
            if self.baseline_hashes.get(relative) == digest:
                continue
            source_file = self.workspace / relative
            target = self.source_workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target)
            changed.append(relative)
        for relative in sorted(set(self.baseline_hashes) - set(current)):
            target = self.source_workspace / relative
            if target.is_file():
                target.unlink()
                changed.append(relative)
        return SyncResult(changed_files=tuple(changed))

    def cleanup(
        self,
        *,
        wipe_workspace: bool,
        remove_image: bool | None = None,
    ) -> None:
        """Remove containers and optionally wipe the ephemeral workspace."""

        if self._cleaned:
            return
        remove = False
        if remove_image is not None:
            remove = remove_image
        elif isinstance(self.executor, DockerExecutor):
            remove = (
                self.executor.spec.remove_image_after_run and not self.executor.image.is_default
            )
        self.executor.cleanup(remove_image=remove)
        if wipe_workspace and self.workspace.exists():
            # Wipe the run workspace root (cloned ``repo/`` for hosted layouts).
            # Sibling ``sessions/`` and ``meta/`` under the job directory are kept
            # so diffs, summaries, and session evidence survive after completion.
            shutil.rmtree(self.workspace, ignore_errors=True)
        self._cleaned = True


def should_wipe_workspace(status: str) -> bool:
    """Return whether a run status allows deleting the ephemeral repository tree.

    Completed jobs wipe the cloned repo after artifacts are persisted. Failed and
    cancelled jobs keep the full workspace (including ``repo/``) for diagnosis —
    matching local CLI retention and publish-failure debugging needs.
    """

    return status == "completed"


def _copy_workspace(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)

    def _ignore(directory: str, names: list[str]) -> set[str]:
        ignored = set(_COPY_IGNORE(directory, names))
        path = Path(directory)
        if path == source / ".gca":
            ignored.update({"sessions", "jobs", "runs"})
        return ignored

    shutil.copytree(source, destination, ignore=_ignore, symlinks=True)


def _hash_tree(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if _skip_relative(relative):
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        hashes[relative] = digest
    return hashes


def _skip_relative(relative: str) -> bool:
    parts = Path(relative).parts
    if not parts:
        return True
    if parts[0] in {".gca", ".git", ".venv", "venv", "node_modules", "__pycache__"}:
        return True
    return any(part == "__pycache__" or part.endswith(".pyc") for part in parts)
