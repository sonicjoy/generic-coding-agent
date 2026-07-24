"""Helpers for creating disposable git repositories in tests."""

from __future__ import annotations

import subprocess
from pathlib import Path


def run_git(cwd: Path, *args: str) -> str:
    """Run a git command in ``cwd`` and return combined stdout/stderr."""

    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {output.strip()}")
    return (result.stdout or "").strip()


def init_repository(
    root: Path,
    *,
    name: str = "repo",
    initial_file: str = "README.md",
    initial_content: str = "hello\n",
    commit_message: str = "initial",
) -> Path:
    """Create a fresh git repository with one committed file and return its path."""

    repository = root / name
    repository.mkdir(parents=True, exist_ok=True)
    run_git(repository, "init")
    run_git(repository, "config", "user.email", "test@example.com")
    run_git(repository, "config", "user.name", "Test")
    (repository / initial_file).write_text(initial_content, encoding="utf-8")
    run_git(repository, "add", initial_file)
    run_git(repository, "commit", "-m", commit_message)
    run_git(repository, "branch", "-M", "main")
    return repository
