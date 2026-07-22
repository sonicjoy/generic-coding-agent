"""Temporary askpass environments for service-owned Git operations."""

from __future__ import annotations

import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

_ASKPASS = """#!/usr/bin/env python3
import os
import sys

prompt = sys.argv[1] if len(sys.argv) > 1 else ""
name = "GCA_GIT_USERNAME" if "username" in prompt.lower() else "GCA_GIT_TOKEN"
print(os.environ[name])
"""


@dataclass(frozen=True)
class GitCredentials:
    """Username/token pair scoped to one service-owned Git subprocess."""

    username: str
    token: str


@contextmanager
def git_credential_env(
    base_env: Mapping[str, str],
    credentials: GitCredentials | None,
) -> Iterator[dict[str, str]]:
    """Yield an environment with a temporary askpass helper when needed."""

    environment = dict(base_env)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    if credentials is None:
        yield environment
        return
    with tempfile.TemporaryDirectory(prefix="gca-askpass-") as temporary:
        askpass = Path(temporary) / "askpass.py"
        askpass.write_text(_ASKPASS, encoding="utf-8")
        askpass.chmod(0o700)
        environment.update(
            {
                "GIT_ASKPASS": str(askpass),
                "GIT_TERMINAL_PROMPT": "0",
                "GCA_GIT_USERNAME": credentials.username,
                "GCA_GIT_TOKEN": credentials.token,
            }
        )
        yield environment
