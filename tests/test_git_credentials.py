from __future__ import annotations

import subprocess
from pathlib import Path

from gca.git_credentials import GitCredentials, git_credential_env


def test_git_credentials_are_scoped_to_temporary_askpass_environment() -> None:
    askpass_path: Path | None = None
    with git_credential_env(
        {"PATH": "/usr/bin:/bin"},
        GitCredentials(username="oauth2", token="secret-token", host="git.example"),
    ) as environment:
        askpass_path = Path(environment["GIT_ASKPASS"])
        username = subprocess.run(
            [str(askpass_path), "Username for https://git.example"],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        token = subprocess.run(
            [str(askpass_path), "Password for https://git.example"],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert username == "oauth2"
        assert token == "secret-token"
        mismatch = subprocess.run(
            [str(askpass_path), "Password for https://git.example.attacker.test"],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert mismatch.returncode != 0
    assert askpass_path is not None
    assert not askpass_path.exists()


def test_askpass_accepts_username_embedded_https_password_prompt() -> None:
    """Git prompts often look like: Password for 'https://x-access-token@host'."""

    with git_credential_env(
        {"PATH": "/usr/bin:/bin"},
        GitCredentials(username="x-access-token", token="secret-token", host="github.com"),
    ) as environment:
        askpass = Path(environment["GIT_ASKPASS"])
        token = subprocess.run(
            [str(askpass), "Password for 'https://x-access-token@github.com':"],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert token == "secret-token"
        mismatch = subprocess.run(
            [str(askpass), "Password for 'https://x-access-token@evil.example':"],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert mismatch.returncode != 0
        assert "host mismatch" in (mismatch.stderr or "")
