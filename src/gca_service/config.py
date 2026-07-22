"""Environment-based hosted-service configuration."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


class ServiceConfigError(ValueError):
    """Raised when hosted-service settings are unsafe or incomplete."""


@dataclass(frozen=True)
class ServiceSettings:
    """Configuration shared by API and worker processes."""

    data_dir: Path
    api_token: str = field(repr=False)
    allowed_repository_hosts: frozenset[str] = frozenset()
    allowed_github_projects: frozenset[str] = frozenset()
    allowed_gitlab_projects: frozenset[str] = frozenset()
    github_webhook_secret: str = field(default="", repr=False)
    gitlab_webhook_secret: str = field(default="", repr=False)
    github_token: str = field(default="", repr=False)
    gitlab_token: str = field(default="", repr=False)
    github_api_url: str = "https://api.github.com"
    gitlab_api_url: str = "https://gitlab.com/api/v4"
    github_host: str = "github.com"
    gitlab_host: str = "gitlab.com"
    lease_seconds: int = 1800
    poll_seconds: float = 2.0
    worker_id: str = field(default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}")
    allow_local_repositories: bool = False
    max_request_bytes: int = 1_000_000
    model_paths: tuple[Path, ...] = ()
    plugin_dir: Path | None = None

    @property
    def database_path(self) -> Path:
        return self.data_dir / "jobs.sqlite3"

    @property
    def workspace_root(self) -> Path:
        return self.data_dir / "workspaces"

    @classmethod
    def from_environment(
        cls,
        environ: dict[str, str] | None = None,
    ) -> ServiceSettings:
        """Build fail-closed settings from environment variables."""

        values = dict(environ or os.environ)
        api_token = values.get("GCA_API_TOKEN", "")
        if not api_token:
            raise ServiceConfigError("GCA_API_TOKEN is required")
        settings = cls(
            data_dir=Path(values.get("GCA_DATA_DIR", ".gca-service")).resolve(),
            api_token=api_token,
            allowed_repository_hosts=_csv(values.get("GCA_ALLOWED_REPOSITORY_HOSTS", "")),
            allowed_github_projects=_csv(values.get("GCA_ALLOWED_GITHUB_PROJECTS", "")),
            allowed_gitlab_projects=_csv(values.get("GCA_ALLOWED_GITLAB_PROJECTS", "")),
            github_webhook_secret=values.get("GCA_GITHUB_WEBHOOK_SECRET", ""),
            gitlab_webhook_secret=values.get("GCA_GITLAB_WEBHOOK_SECRET", ""),
            github_token=values.get("GCA_GITHUB_TOKEN", ""),
            gitlab_token=values.get("GCA_GITLAB_TOKEN", ""),
            github_api_url=values.get("GCA_GITHUB_API_URL", "https://api.github.com"),
            gitlab_api_url=values.get("GCA_GITLAB_API_URL", "https://gitlab.com/api/v4"),
            github_host=values.get("GCA_GITHUB_HOST", "github.com").lower(),
            gitlab_host=values.get("GCA_GITLAB_HOST", "gitlab.com").lower(),
            lease_seconds=_integer(values.get("GCA_LEASE_SECONDS", "1800"), "GCA_LEASE_SECONDS"),
            poll_seconds=_positive_float(
                values.get("GCA_POLL_SECONDS", "2"),
                "GCA_POLL_SECONDS",
            ),
            worker_id=values.get("GCA_WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}",
            allow_local_repositories=_boolean(values.get("GCA_ALLOW_LOCAL_REPOSITORIES", "false")),
            max_request_bytes=_integer(
                values.get("GCA_MAX_REQUEST_BYTES", "1000000"),
                "GCA_MAX_REQUEST_BYTES",
            ),
            model_paths=tuple(
                Path(value).resolve()
                for value in values.get("GCA_MODEL_CONFIG_PATHS", "").split(os.pathsep)
                if value
            ),
            plugin_dir=(
                Path(values["GCA_PLUGIN_DIR"]).resolve()
                if values.get("GCA_PLUGIN_DIR")
                else None
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        """Validate secret/allowlist pairings before accepting requests."""

        if not self.api_token:
            raise ServiceConfigError("api_token is required")
        if len(self.api_token) < 16:
            raise ServiceConfigError("api_token must be at least 16 characters")
        if not self.allowed_repository_hosts and not self.allow_local_repositories:
            raise ServiceConfigError(
                "configure allowed_repository_hosts or explicitly allow local repositories"
            )
        if self.github_webhook_secret and not self.allowed_github_projects:
            raise ServiceConfigError(
                "GitHub webhook secret requires an explicit GitHub project allowlist"
            )
        if self.gitlab_webhook_secret and not self.allowed_gitlab_projects:
            raise ServiceConfigError(
                "GitLab webhook secret requires an explicit GitLab project allowlist"
            )
        if self.github_webhook_secret and len(self.github_webhook_secret) < 16:
            raise ServiceConfigError("GitHub webhook secret must be at least 16 characters")
        if self.gitlab_webhook_secret and len(self.gitlab_webhook_secret) < 16:
            raise ServiceConfigError("GitLab webhook secret must be at least 16 characters")
        if self.lease_seconds <= 0:
            raise ServiceConfigError("lease_seconds must be positive")
        if self.max_request_bytes <= 0:
            raise ServiceConfigError("max_request_bytes must be positive")
        if not self.github_host or not self.gitlab_host:
            raise ServiceConfigError("SCM host names must not be empty")
        _validate_api_url(self.github_api_url, "github_api_url")
        _validate_api_url(self.gitlab_api_url, "gitlab_api_url")
        missing_models = [str(path) for path in self.model_paths if not path.is_file()]
        if missing_models:
            raise ServiceConfigError(
                f"model config paths do not exist: {', '.join(missing_models)}"
            )
        if self.plugin_dir is not None and not self.plugin_dir.is_dir():
            raise ServiceConfigError(f"plugin directory does not exist: {self.plugin_dir}")


def _csv(value: str) -> frozenset[str]:
    return frozenset(item.strip() for item in value.split(",") if item.strip())


def _integer(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ServiceConfigError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise ServiceConfigError(f"{name} must be positive")
    return parsed


def _positive_float(value: str, name: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ServiceConfigError(f"{name} must be numeric") from exc
    if parsed <= 0:
        raise ServiceConfigError(f"{name} must be positive")
    return parsed


def _boolean(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise ServiceConfigError("boolean settings must be true or false")


def _validate_api_url(value: str, name: str) -> None:
    parsed = urlparse(value)
    if not parsed.hostname:
        raise ServiceConfigError(f"{name} must include a host")
    local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
        raise ServiceConfigError(f"{name} must use HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ServiceConfigError(f"{name} must not include credentials, query, or fragment")
