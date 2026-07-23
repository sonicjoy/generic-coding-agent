from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gca.executor.fake import FakeExecutor
from gca.integrations.scm import ChangeRequest, PublicationController, PublicationError
from gca.jobs.models import Job, PublicationTarget, RepositorySpec, RunSpec
from gca.repo_config import load_repo_config


class FakeAdapter:
    provider = "fake"

    def __init__(self) -> None:
        self.pushed: list[str] = []
        self.requests: list[ChangeRequest] = []

    def supports_repository(self, repository_url: str) -> bool:
        return True

    def push(self, workspace: Path, branch: str, repository_url: str) -> None:
        self.pushed.append(branch)

    def open_change_request(self, request: ChangeRequest) -> str:
        self.requests.append(request)
        return "https://scm.example/change/1"


def _git(workspace: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.email", "tests@example.test")
    _git(repository, "config", "user.name", "Tests")
    config_dir = repository / ".gca"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        """
version: 1
tools:
  fixed_commands:
    verify:
      argv: [python, -c, "print('verified')"]
publication:
  required_checks: [verify]
  allowed_paths: ["src/**"]
  max_files: 5
  max_changed_lines: 20
""",
        encoding="utf-8",
    )
    (repository / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Initial")
    return repository


def _job(repository: Path) -> Job:
    return Job(
        id="a" * 32,
        run_spec=RunSpec(
            task="Add a useful change",
            repository=RepositorySpec(str(repository), ref="main"),
            publication=PublicationTarget(provider="fake", base_ref="main"),
        ),
        session_id="session-1",
    )


def _executor() -> FakeExecutor:
    return FakeExecutor(execute_locally=True)


def test_controller_checks_commits_pushes_and_opens_change_request(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    source = repository / "src"
    source.mkdir()
    (source / "change.py").write_text("VALUE = 1\n", encoding="utf-8")
    adapter = FakeAdapter()

    result = PublicationController({"fake": adapter}).publish(
        _job(repository),
        repository,
        load_repo_config(repository),
        executor=_executor(),
    )

    assert result["change_request_url"] == "https://scm.example/change/1"
    assert result["branch"] == "gca/aaaaaaaaaaaa"
    assert adapter.pushed == ["gca/aaaaaaaaaaaa"]
    assert adapter.requests[0].target_branch == "main"
    assert _git(repository, "log", "-1", "--pretty=%s") == "gca: Add a useful change"


def test_controller_skips_empty_change_request(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    adapter = FakeAdapter()

    result = PublicationController({"fake": adapter}).publish(
        _job(repository),
        repository,
        load_repo_config(repository),
        executor=_executor(),
    )

    assert result["no_changes"] is True
    assert adapter.pushed == []
    assert adapter.requests == []


def test_controller_rejects_disallowed_paths(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / ".env").write_text("SECRET=value\n", encoding="utf-8")

    with pytest.raises(PublicationError, match="protected path"):
        PublicationController({"fake": FakeAdapter()}).publish(
            _job(repository),
            repository,
            load_repo_config(repository),
            executor=_executor(),
        )


def test_controller_uses_immutable_pre_run_policy_snapshot(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    snapshot = load_repo_config(repository)
    (repository / ".gca" / "config.yaml").write_text(
        """
version: 1
tools:
  fixed_commands:
    injected:
      argv: [python, -c, "from pathlib import Path; Path('owned.txt').write_text('bad')"]
publication:
  required_checks: [injected]
""",
        encoding="utf-8",
    )
    source = repository / "src"
    source.mkdir()
    (source / "change.py").write_text("VALUE = 1\n", encoding="utf-8")

    with pytest.raises(PublicationError, match="protected path"):
        PublicationController({"fake": FakeAdapter()}).publish(
            _job(repository),
            repository,
            snapshot,
            executor=_executor(),
        )

    assert not (repository / "owned.txt").exists()


def test_publication_secret_grants_are_project_and_tool_scoped(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    repository = _repository(tmp_path)
    (repository / ".gca" / "config.yaml").write_text(
        """
version: 1
tools:
  secret_access:
    verify: [DATABASE_URL]
  fixed_commands:
    verify:
      argv: [python, --version]
publication:
  required_checks: [verify]
  allowed_paths: ["src/**"]
""",
        encoding="utf-8",
    )
    _git(repository, "add", ".gca/config.yaml")
    _git(repository, "commit", "-m", "Configure scoped check")
    source = repository / "src"
    source.mkdir()
    (source / "change.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", "secret")  # type: ignore[attr-defined]

    with pytest.raises(PublicationError, match="unapproved publication secret grants"):
        PublicationController({"fake": FakeAdapter()}).publish(
            _job(repository),
            repository,
            load_repo_config(repository),
            executor=_executor(),
        )
