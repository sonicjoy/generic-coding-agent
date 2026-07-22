"""Credential-scoped Git push helper used only by SCM adapters."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from gca.credentials import CredentialBroker
from gca.integrations.scm import PublicationError

_ASKPASS = """#!/usr/bin/env python3
import os
import sys

prompt = sys.argv[1] if len(sys.argv) > 1 else ""
name = "GCA_GIT_USERNAME" if "username" in prompt.lower() else "GCA_GIT_TOKEN"
print(os.environ[name])
"""


def push_with_token(
    workspace: Path,
    branch: str,
    *,
    username: str,
    token: str,
) -> None:
    """Push a branch without placing credentials in argv or the remote URL."""

    broker = CredentialBroker({"SCM_TOKEN": token})
    with tempfile.TemporaryDirectory(prefix="gca-askpass-") as temporary:
        askpass = Path(temporary) / "askpass.py"
        askpass.write_text(_ASKPASS, encoding="utf-8")
        askpass.chmod(0o700)
        env = broker.subprocess_env("hosted")
        env.update(
            {
                "GIT_ASKPASS": str(askpass),
                "GIT_TERMINAL_PROMPT": "0",
                "GCA_GIT_USERNAME": username,
                "GCA_GIT_TOKEN": token,
            }
        )
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
        raise PublicationError(f"git push failed: {output.strip()}")
