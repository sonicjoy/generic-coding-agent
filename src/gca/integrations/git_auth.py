"""Credential-scoped Git push helper used only by SCM adapters."""

from __future__ import annotations

import subprocess
from pathlib import Path
from urllib.parse import urlparse

from gca.credentials import CredentialBroker
from gca.git_credentials import GitCredentials, git_credential_env
from gca.integrations.scm import PublicationError


def push_with_token(
    workspace: Path,
    branch: str,
    *,
    repository_url: str,
    username: str,
    token: str,
) -> None:
    """Push a branch without placing credentials in argv or the remote URL."""

    parsed = urlparse(repository_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise PublicationError("token-authenticated publication requires an HTTPS repository URL")
    broker = CredentialBroker({"SCM_TOKEN": token})
    with git_credential_env(
        broker.subprocess_env("hosted"),
        GitCredentials(username=username, token=token, host=parsed.hostname),
    ) as env:
        result = subprocess.run(
            [
                "git",
                "-c",
                "credential.helper=",
                "-c",
                "http.followRedirects=false",
                "push",
                "-u",
                "--",
                repository_url,
                f"{branch}:refs/heads/{branch}",
            ],
            shell=False,
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    if result.returncode != 0:
        output = broker.redact((result.stdout or "") + (result.stderr or ""))
        raise PublicationError(
            f"git push failed: {output.strip()}",
            retryable=True,
        )
