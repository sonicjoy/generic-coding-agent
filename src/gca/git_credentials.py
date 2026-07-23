"""Temporary askpass environments for service-owned Git operations."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

_ASKPASS = """#!/usr/bin/env python3
import os
import re
import sys
from urllib.parse import urlparse

prompt = sys.argv[1] if len(sys.argv) > 1 else ""
expected_host = os.environ.get("GCA_GIT_HOST", "")
hosts = {
    urlparse(url).hostname
    for url in re.findall(r"https?://[^\\s'\\"]+", prompt)
}
if expected_host and expected_host.lower() not in {host.lower() for host in hosts if host}:
    raise SystemExit("credential prompt host mismatch")
name = "GCA_GIT_USERNAME" if "username" in prompt.lower() else "GCA_GIT_TOKEN"
print(os.environ[name])
"""


@dataclass(frozen=True)
class GitCredentials:
    """Username/token pair scoped to one service-owned Git subprocess."""

    username: str
    token: str
    host: str


@contextmanager
def git_credential_env(
    base_env: Mapping[str, str],
    credentials: GitCredentials | None,
) -> Iterator[dict[str, str]]:
    """Yield an environment with a temporary askpass helper when needed."""

    environment = dict(base_env)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
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
                "GCA_GIT_HOST": credentials.host,
            }
        )
        yield environment
