from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gca.jobs.models import RepositorySpec
from gca.workspace.prepare import WorkspaceError, prepare_repository


def _git(command: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *command],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


def _source_repository(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    _git(["init", "-b", "main"], source)
    _git(["config", "user.email", "tests@example.test"], source)
    _git(["config", "user.name", "Tests"], source)
    (source / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(["add", "README.md"], source)
    _git(["commit", "-m", "Initial fixture"], source)
    return source


def test_prepare_repository_clones_local_fixture_when_explicitly_allowed(
    tmp_path: Path,
) -> None:
    source = _source_repository(tmp_path)

    checkout = prepare_repository(
        RepositorySpec(url=str(source), ref="main"),
        tmp_path / "checkout",
        allow_local=True,
    )

    assert (checkout / "README.md").read_text(encoding="utf-8") == "fixture\n"
    assert (
        prepare_repository(
            RepositorySpec(url=str(source), ref="main"),
            checkout,
            allow_local=True,
        )
        == checkout
    )


def test_prepare_repository_rejects_local_and_credential_urls(tmp_path: Path) -> None:
    source = _source_repository(tmp_path)

    with pytest.raises(WorkspaceError, match="HTTPS or SSH"):
        prepare_repository(RepositorySpec(str(source)), tmp_path / "checkout")
    with pytest.raises(WorkspaceError, match="credentials"):
        prepare_repository(
            RepositorySpec("https://token@example.test/repo.git"),
            tmp_path / "other",
        )
