from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from gca.integrations import git_auth
from gca.integrations.git_auth import push_with_token


def test_push_uses_explicit_bound_remote_and_disables_redirects(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(git_auth.subprocess, "run", fake_run)  # type: ignore[attr-defined]

    push_with_token(
        tmp_path,
        "gca/job",
        repository_url="https://github.example/owner/repo.git",
        username="x-access-token",
        token="secret",
    )

    argv = calls[0]
    assert "credential.helper=" in argv
    assert "http.followRedirects=false" in argv
    assert "https://github.example/owner/repo.git" in argv
    assert "origin" not in argv
    assert "secret" not in argv
