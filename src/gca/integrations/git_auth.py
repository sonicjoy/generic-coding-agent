"""Credential-scoped Git push helper used only by SCM adapters."""

from __future__ import annotations

import subprocess
import tempfile
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
    base_env = broker.subprocess_env("hosted")
    with tempfile.TemporaryDirectory(prefix="gca-publish-") as temporary:
        mirror = Path(temporary) / "repository.git"
        with git_credential_env(base_env, None) as clone_env:
            clone = subprocess.run(
                [
                    "git",
                    "clone",
                    "--bare",
                    "--no-local",
                    "--no-tags",
                    "--branch",
                    branch,
                    "--",
                    str(workspace),
                    str(mirror),
                ],
                shell=False,
                env=clone_env,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        if clone.returncode != 0:
            output = broker.redact((clone.stdout or "") + (clone.stderr or ""))
            raise PublicationError(f"could not prepare clean Git metadata: {output.strip()}")

        with git_credential_env(
            base_env,
            GitCredentials(username=username, token=token, host=parsed.hostname),
        ) as push_env:
            push = subprocess.run(
                [
                    "git",
                    f"--git-dir={mirror}",
                    "-c",
                    "credential.helper=",
                    "-c",
                    "http.followRedirects=false",
                    "push",
                    "--",
                    repository_url,
                    f"{branch}:refs/heads/{branch}",
                ],
                shell=False,
                env=push_env,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        if push.returncode != 0:
            output = broker.redact((push.stdout or "") + (push.stderr or ""))
            raise PublicationError(
                f"git push failed: {output.strip()}",
                retryable=True,
            )
