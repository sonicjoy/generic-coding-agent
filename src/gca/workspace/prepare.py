"""Safe repository cloning into isolated job workspaces."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import unquote, urlparse

from gca.jobs.models import RepositorySpec

_SCP_STYLE = re.compile(r"^[A-Za-z0-9_.-]+@(?P<host>[A-Za-z0-9.-]+):(?P<path>.+)$")


class WorkspaceError(RuntimeError):
    """Raised when a repository workspace cannot be prepared."""


def prepare_repository(
    spec: RepositorySpec,
    destination: Path,
    *,
    allowed_hosts: frozenset[str] = frozenset(),
    allow_local: bool = False,
    env: Mapping[str, str] | None = None,
    timeout: int = 300,
) -> Path:
    """Clone ``spec`` using argv execution and return the checkout path."""

    validate_repository_spec(spec, allowed_hosts=allowed_hosts, allow_local=allow_local)
    destination = Path(destination).resolve()
    if (destination / ".git").is_dir():
        return destination
    if destination.exists() and any(destination.iterdir()):
        raise WorkspaceError(f"workspace destination is not empty: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        "git",
        "clone",
        "--depth",
        str(spec.shallow_depth),
        "--single-branch",
        "--branch",
        spec.ref,
        "--",
        spec.url,
        str(destination),
    ]
    try:
        result = subprocess.run(
            argv,
            shell=False,
            env=dict(env) if env is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceError(f"repository clone failed: {exc}") from exc
    if result.returncode != 0:
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        raise WorkspaceError(f"repository clone failed with exit {result.returncode}: {output}")
    return destination


def validate_repository_spec(
    spec: RepositorySpec,
    *,
    allowed_hosts: frozenset[str],
    allow_local: bool,
) -> None:
    """Validate repository URL, ref, depth, and host policy without cloning."""

    if not spec.url.strip():
        raise WorkspaceError("repository URL must not be empty")
    if not spec.ref.strip() or spec.ref.startswith("-"):
        raise WorkspaceError("repository ref is invalid")
    if not 1 <= spec.shallow_depth <= 1000:
        raise WorkspaceError("repository shallow depth must be from 1 to 1000")

    parsed = urlparse(spec.url)
    host: str | None = None
    if parsed.scheme in {"https", "ssh"}:
        if parsed.password is not None or (
            parsed.scheme == "https" and parsed.username is not None
        ):
            raise WorkspaceError("repository URL must not contain credentials")
        host = parsed.hostname
    elif parsed.scheme == "file":
        if not allow_local:
            raise WorkspaceError("local repository URLs are disabled")
        local_path = Path(unquote(parsed.path))
        if not local_path.exists():
            raise WorkspaceError(f"local repository does not exist: {local_path}")
    elif not parsed.scheme:
        ssh_match = _SCP_STYLE.fullmatch(spec.url)
        if ssh_match is not None:
            host = ssh_match.group("host")
        elif allow_local and Path(spec.url).exists():
            host = None
        else:
            raise WorkspaceError("repository must use HTTPS or SSH")
    else:
        raise WorkspaceError(f"unsupported repository URL scheme: {parsed.scheme}")
    if allowed_hosts and (
        host is None or host.lower() not in {item.lower() for item in allowed_hosts}
    ):
        raise WorkspaceError(f"repository host is not allowed: {host or '(local)'}")
