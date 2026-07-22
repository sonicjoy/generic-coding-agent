"""SCM repository URL parsing helpers."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

_SCP_STYLE = re.compile(r"^[A-Za-z0-9_.-]+@[A-Za-z0-9.-]+:(?P<path>.+)$")


def repository_path(url: str) -> str:
    """Return an owner/group and repository path from HTTPS or SSH URLs."""

    parsed = urlparse(url)
    if parsed.scheme in {"https", "ssh"}:
        path = unquote(parsed.path)
    else:
        match = _SCP_STYLE.fullmatch(url)
        if match is None:
            raise ValueError(f"cannot derive repository path from URL: {url}")
        path = match.group("path")
    normalized = path.strip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    if "/" not in normalized:
        raise ValueError(f"repository URL has no owner/group path: {url}")
    return normalized
