"""Workspace path validation shared by repository configuration and tools."""

from __future__ import annotations

from pathlib import Path

IGNORED_DIRS = frozenset({".git", ".venv", "venv", "__pycache__", "node_modules", ".gca"})


class WorkspacePathError(ValueError):
    """Raised when a configured path escapes its repository workspace."""


def resolve_workspace_path(workspace: Path, value: str, *, label: str = "path") -> Path:
    """Resolve a repository-relative path and reject workspace escapes."""

    raw = Path(value)
    if raw.is_absolute():
        raise WorkspacePathError(f"{label} must be relative to the workspace: {value!r}")
    root = Path(workspace).resolve()
    target = (root / raw).resolve()
    if target != root and root not in target.parents:
        raise WorkspacePathError(f"{label} escapes the workspace: {value!r}")
    return target
