from __future__ import annotations

from pathlib import Path
from typing import Any

from gca.integrations import github
from gca.integrations.github import GitHubScmAdapter
from gca.integrations.http import IntegrationHttpError
from gca.jobs.models import RepositorySpec, RunSpec
from gca_service.config import ServiceSettings
from gca_service.issue_progress import announce_github_issue_start
from gca_service.state import ServiceState


def test_announce_assigns_and_comments_when_enabled(tmp_path: Path, monkeypatch: object) -> None:
    settings = ServiceSettings(
        data_dir=tmp_path / "data",
        api_token="api-token-123456",
        allow_local_repositories=True,
        github_token="gh-token",
        github_issue_assign=True,
        github_issue_progress_comments=True,
        github_bot_user="gca-bot",
    )
    state = ServiceState.build(settings)
    job = state.store.create(
        RunSpec(
            task="Fix typo",
            repository=RepositorySpec(url="https://github.com/owner/repo.git", ref="main"),
            labels={"provider": "github", "issue_id": "12", "source": "issues.labeled"},
        )
    )
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake_request(
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: dict[str, Any] | None = None,
    ) -> Any:
        calls.append((method, url, body))
        if url.endswith("/assignees"):
            return {"assignees": [{"login": "gca-bot"}]}
        if url.endswith("/comments"):
            return {"html_url": "https://github.com/owner/repo/issues/12#issuecomment-1"}
        return {}

    monkeypatch.setattr(github, "request_json", fake_request)  # type: ignore[attr-defined]
    events: list[str] = []
    announce_github_issue_start(
        job,
        settings,
        on_event=events.append,
        adapter=GitHubScmAdapter("gh-token"),
    )

    assert any(call[0] == "POST" and call[1].endswith("/assignees") for call in calls)
    assert any(call[0] == "POST" and call[1].endswith("/comments") for call in calls)
    comment_body = next(call[2] for call in calls if call[1].endswith("/comments"))
    assert comment_body is not None
    assert job.id in comment_body["body"]
    assert any("event=issue_assigned" in event for event in events)
    assert any("event=issue_comment" in event for event in events)


def test_announce_errors_do_not_raise(tmp_path: Path, monkeypatch: object) -> None:
    settings = ServiceSettings(
        data_dir=tmp_path / "data",
        api_token="api-token-123456",
        allow_local_repositories=True,
        github_token="gh-token",
        github_issue_assign=True,
    )
    state = ServiceState.build(settings)
    job = state.store.create(
        RunSpec(
            task="Fix typo",
            repository=RepositorySpec(url="https://github.com/owner/repo.git", ref="main"),
            labels={"provider": "github", "issue_id": "12"},
        )
    )

    def boom(*args: object, **kwargs: object) -> object:
        raise IntegrationHttpError("integration request failed with HTTP 403: missing issues")

    monkeypatch.setattr(github, "request_json", boom)  # type: ignore[attr-defined]
    events: list[str] = []
    announce_github_issue_start(
        job,
        settings,
        on_event=events.append,
        adapter=GitHubScmAdapter("gh-token"),
    )
    assert any("event=issue_progress_error" in event and "403" in event for event in events)


def test_github_adapter_issue_helpers(monkeypatch: object) -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake_request(
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: dict[str, Any] | None = None,
    ) -> Any:
        calls.append((method, url, body))
        if url.endswith("/user"):
            return {"login": "gca-bot"}
        return {"html_url": "https://github.com/owner/repo/issues/7#issuecomment-9"}

    monkeypatch.setattr(github, "request_json", fake_request)  # type: ignore[attr-defined]
    adapter = GitHubScmAdapter("token")
    assert adapter.authenticated_login() == "gca-bot"
    adapter.assign_issue("https://github.com/owner/repo.git", "7", ["gca-bot"])
    url = adapter.create_issue_comment(
        "https://github.com/owner/repo.git",
        "7",
        "GCA started job `abc`.",
    )
    assert url.endswith("issuecomment-9")
    assert any("/user" in call[1] for call in calls)
