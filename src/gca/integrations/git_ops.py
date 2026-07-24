"""Shared credential-aware git subprocess helpers for publication paths."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path

from gca.credentials import CredentialBroker


def run_git(
    workspace: Path,
    args: list[str],
    credentials: CredentialBroker,
    *,
    isolated_config: bool = False,
    error_factory: Callable[[str], Exception] | None = None,
) -> str:
    """Run ``git`` with hosted credentials and return combined stdout/stderr.

    When ``isolated_config`` is true, ignore global/system git config and allow
    any workspace via ``safe.directory=*`` (used by trusted issue-session
    publication).
    """

    env = credentials.subprocess_env("hosted")
    command = ["git", *args]
    if isolated_config:
        env = dict(env)
        env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        env["GIT_CONFIG_SYSTEM"] = "/dev/null"
        command = ["git", "-c", "safe.directory=*", *args]
    result = subprocess.run(
        command,
        shell=False,
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    output = credentials.redact((result.stdout or "") + (result.stderr or ""))
    if result.returncode != 0:
        detail = f"$ git {shlex.join(args)}\n{output.strip()}"
        if error_factory is not None:
            raise error_factory(detail)
        raise RuntimeError(detail)
    return output
