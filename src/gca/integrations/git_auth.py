"""Credential-scoped Git push helper used only by SCM adapters."""

from __future__ import annotations

import subprocess
from pathlib import Path

from gca.credentials import CredentialBroker
from gca.git_credentials import GitCredentials, git_credential_env
from gca.integrations.scm import PublicationError


def push_with_token(
    workspace: Path,
    branch: str,
    *,
    username: str,
    token: str,
) -> None:
    """Push a branch without placing credentials in argv or the remote URL."""

    broker = CredentialBroker({"SCM_TOKEN": token})
    with git_credential_env(
        broker.subprocess_env("hosted"),
        GitCredentials(username=username, token=token),
    ) as env:
        result = subprocess.run(
            ["git", "push", "-u", "origin", branch],
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
