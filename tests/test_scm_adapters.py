from __future__ import annotations

from typing import Any

from gca.integrations import github, gitlab
from gca.integrations.github import GitHubScmAdapter
from gca.integrations.gitlab import GitLabScmAdapter
from gca.integrations.scm import ChangeRequest


def _request(url: str) -> ChangeRequest:
    return ChangeRequest(
        repository_url=url,
        source_branch="gca/job",
        target_branch="main",
        title="gca: test",
        body="body",
        draft=False,
        commit_sha="abc",
    )


def test_github_adapter_reuses_existing_pull_request(monkeypatch: object) -> None:
    calls: list[tuple[str, str]] = []

    def fake_request(
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: dict[str, Any] | None = None,
    ) -> Any:
        calls.append((method, url))
        return [{"html_url": "https://github.example/pull/1"}]

    monkeypatch.setattr(github, "request_json", fake_request)  # type: ignore[attr-defined]

    result = GitHubScmAdapter("token").open_change_request(
        _request("https://github.com/owner/repo.git")
    )

    assert result == "https://github.example/pull/1"
    assert calls[0][0] == "GET"
    assert len(calls) == 1


def test_github_adapter_links_branch_to_issue(monkeypatch: object) -> None:
    calls: list[dict[str, Any] | None] = []

    def fake_request(
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: dict[str, Any] | None = None,
    ) -> Any:
        _ = method, url, headers
        calls.append(body)
        if body is not None and str(body["query"]).startswith("query"):
            return {
                "data": {
                    "repository": {
                        "id": "R_123",
                        "issue": {"id": "I_123"},
                        "defaultBranchRef": {"target": {"oid": "base"}},
                    }
                }
            }
        return {
            "data": {
                "createLinkedBranch": {
                    "linkedBranch": {"ref": {"name": "gca/job"}},
                }
            }
        }

    monkeypatch.setattr(github, "request_json", fake_request)  # type: ignore[attr-defined]

    linked = GitHubScmAdapter("token").link_branch_to_issue(
        "https://github.com/owner/repo.git",
        "gca/job",
        "12",
        "abc123",
    )

    assert linked is True
    assert calls[1] is not None
    mutation = str(calls[1]["query"])
    assert mutation.count("{") == mutation.count("}")
    assert calls[1]["variables"]["input"] == {
        "issueId": "I_123",
        "name": "gca/job",
        "oid": "abc123",
        "repositoryId": "R_123",
    }


def test_gitlab_adapter_creates_merge_request_when_missing(monkeypatch: object) -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake_request(
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: dict[str, Any] | None = None,
    ) -> Any:
        calls.append((method, url, body))
        if method == "GET":
            return []
        return {"web_url": "https://gitlab.example/merge_requests/1"}

    monkeypatch.setattr(gitlab, "request_json", fake_request)  # type: ignore[attr-defined]

    result = GitLabScmAdapter("token").open_change_request(
        _request("git@gitlab.com:group/nested/repo.git")
    )

    assert result == "https://gitlab.example/merge_requests/1"
    assert [call[0] for call in calls] == ["GET", "POST"]
    assert calls[1][2] is not None
    assert calls[1][2]["source_branch"] == "gca/job"


def test_scm_adapters_bind_tokens_to_expected_https_host() -> None:
    github_adapter = GitHubScmAdapter("token", git_host="github.example")
    gitlab_adapter = GitLabScmAdapter("token", git_host="gitlab.example")

    assert github_adapter.supports_repository("https://github.example/owner/repo.git")
    assert not github_adapter.supports_repository("https://attacker.example/owner/repo.git")
    assert not github_adapter.supports_repository("git@github.example:owner/repo.git")
    assert gitlab_adapter.supports_repository("https://gitlab.example/group/repo.git")
