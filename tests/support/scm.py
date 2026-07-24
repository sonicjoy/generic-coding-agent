"""Shared SCM adapter doubles for publication tests."""

from __future__ import annotations

from pathlib import Path

from gca.integrations.scm import ChangeRequest


class FakeScmAdapter:
    """In-memory SCM adapter that records push/link/open-change-request calls."""

    provider = "fake"

    def __init__(self) -> None:
        self.pushed: list[str] = []
        self.linked: list[tuple[str, str, str]] = []
        self.requests: list[ChangeRequest] = []

    def supports_repository(self, repository_url: str) -> bool:
        return True

    def push(self, workspace: Path, branch: str, repository_url: str) -> None:
        _ = workspace, repository_url
        self.pushed.append(branch)

    def link_branch_to_issue(
        self,
        repository_url: str,
        branch: str,
        issue_id: str,
        oid: str,
    ) -> bool:
        _ = repository_url
        self.linked.append((branch, issue_id, oid))
        return True

    def open_change_request(self, request: ChangeRequest) -> str:
        self.requests.append(request)
        return "https://scm.example/change/1"
