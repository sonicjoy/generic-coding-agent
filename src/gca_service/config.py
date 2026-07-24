"""Environment-based hosted-service configuration."""

from __future__ import annotations

import json
import os
import re
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from gca.integrations.webhook_registration import (
    RegistrationError,
    WebhookRegistration,
    parse_registrations,
)


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
    publish_mode: str = "auto"
    github_api_url: str = "https://api.github.com"
    gitlab_api_url: str = "https://gitlab.com/api/v4"
    github_host: str = "github.com"
    gitlab_host: str = "gitlab.com"
    github_trigger_label: str = "gca-run"
    gitlab_trigger_label: str = "gca-run"
    lease_seconds: int = 1800
    poll_seconds: float = 2.0
    worker_id: str = field(default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}")
    allow_local_repositories: bool = False
    max_request_bytes: int = 1_000_000
    model_paths: tuple[Path, ...] = ()
    plugin_dir: Path | None = None
    tool_secret_grants: dict[str, dict[str, frozenset[str]]] = field(default_factory=dict)
    gitlab_webhook_registrations: dict[str, WebhookRegistration] = field(default_factory=dict)
    allow_auto_merge_projects: frozenset[int] = frozenset()
    workspace_retention_seconds: int = 86400
    log_retention_seconds: int = 2_592_000
    bot_user_id: int | None = None
    membership_access_levels: dict[tuple[int, int], int] = field(default_factory=dict)
    default_max_steps: int | None = None
    github_issue_assign: bool = False
    github_issue_progress_comments: bool = False
    github_bot_user: str = ""

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

        values = dict(os.environ if environ is None else environ)
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
            publish_mode=values.get("GCA_PUBLISH_MODE", "auto").strip().lower() or "auto",
            github_api_url=values.get("GCA_GITHUB_API_URL", "https://api.github.com"),
            gitlab_api_url=values.get("GCA_GITLAB_API_URL", "https://gitlab.com/api/v4"),
            github_host=values.get("GCA_GITHUB_HOST", "github.com").lower(),
            gitlab_host=values.get("GCA_GITLAB_HOST", "gitlab.com").lower(),
            github_trigger_label=values.get("GCA_GITHUB_TRIGGER_LABEL", "gca-run"),
            gitlab_trigger_label=values.get("GCA_GITLAB_TRIGGER_LABEL", "gca-run"),
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
                Path(values["GCA_PLUGIN_DIR"]).resolve() if values.get("GCA_PLUGIN_DIR") else None
            ),
            tool_secret_grants=_secret_grants(values.get("GCA_TOOL_SECRET_GRANTS", "")),
            gitlab_webhook_registrations=_registrations(
                values.get("GCA_GITLAB_WEBHOOK_REGISTRATIONS", "")
            ),
            allow_auto_merge_projects=_int_csv(values.get("GCA_ALLOW_AUTO_MERGE_PROJECTS", "")),
            workspace_retention_seconds=_nonnegative_integer(
                values.get("GCA_WORKSPACE_RETENTION_SECONDS", "86400"),
                "GCA_WORKSPACE_RETENTION_SECONDS",
            ),
            log_retention_seconds=_nonnegative_integer(
                values.get("GCA_LOG_RETENTION_SECONDS", "2592000"),
                "GCA_LOG_RETENTION_SECONDS",
            ),
            bot_user_id=_optional_int(values.get("GCA_GITLAB_BOT_USER_ID")),
            membership_access_levels=_membership_levels(
                values.get("GCA_GITLAB_MEMBERSHIP_LEVELS", "")
            ),
            default_max_steps=_optional_max_steps(values.get("GCA_DEFAULT_MAX_STEPS")),
            github_issue_assign=_boolean(values.get("GCA_GITHUB_ISSUE_ASSIGN", "false")),
            github_issue_progress_comments=_boolean(
                values.get("GCA_GITHUB_ISSUE_PROGRESS_COMMENTS", "false")
            ),
            github_bot_user=values.get("GCA_GITHUB_BOT_USER", "").strip(),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        """Validate secret/allowlist pairings before accepting requests."""

        if not self.api_token:
            raise ServiceConfigError("api_token is required")
        if len(self.api_token) < 16:
            raise ServiceConfigError("api_token must be at least 16 characters")
        if self.publish_mode not in {"auto", "off", "branch", "pr"}:
            raise ServiceConfigError("GCA_PUBLISH_MODE must be 'off', 'branch', 'pr', or 'auto'")
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
        if self.default_max_steps is not None and not 1 <= self.default_max_steps <= 1000:
            raise ServiceConfigError("default_max_steps must be an integer from 1 to 1000")
        if not self.github_host or not self.gitlab_host:
            raise ServiceConfigError("SCM host names must not be empty")
        if not self.github_trigger_label.strip() or not self.gitlab_trigger_label.strip():
            raise ServiceConfigError("SCM trigger labels must not be empty")
        _validate_api_url(self.github_api_url, "github_api_url")
        _validate_api_url(self.gitlab_api_url, "gitlab_api_url")
        missing_models = [str(path) for path in self.model_paths if not path.is_file()]
        if missing_models:
            raise ServiceConfigError(
                f"model config paths do not exist: {', '.join(missing_models)}"
            )
        if self.plugin_dir is not None and not self.plugin_dir.is_dir():
            raise ServiceConfigError(f"plugin directory does not exist: {self.plugin_dir}")
        all_secret_names = {
            name
            for tools in self.tool_secret_grants.values()
            for names in tools.values()
            for name in names
        }
        invalid_secrets = sorted(
            name for name in all_secret_names if re.fullmatch(r"[A-Z_][A-Z0-9_]*", name) is None
        )
        if invalid_secrets:
            raise ServiceConfigError(
                f"invalid allowed tool secret names: {', '.join(invalid_secrets)}"
            )
        reserved = {
            "GCA_API_TOKEN",
            "GCA_GITHUB_TOKEN",
            "GCA_GITHUB_WEBHOOK_SECRET",
            "GCA_GITLAB_TOKEN",
            "GCA_GITLAB_WEBHOOK_SECRET",
        } & all_secret_names
        if reserved:
            raise ServiceConfigError(
                f"service-owned secrets cannot be granted to tools: {', '.join(sorted(reserved))}"
            )
        if self.gitlab_webhook_secret and not self.gitlab_webhook_registrations:
            # Legacy single-secret mode remains valid with project allowlist.
            pass
        if len(self.gitlab_webhook_registrations) > 1 and not all(
            registration.project_id > 0
            for registration in self.gitlab_webhook_registrations.values()
        ):
            raise ServiceConfigError("every GitLab webhook registration needs a project_id")


def _csv(value: str) -> frozenset[str]:
    return frozenset(item.strip() for item in value.split(",") if item.strip())


def _int_csv(value: str) -> frozenset[int]:
    result: set[int] = set()
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            result.add(int(item))
        except ValueError as exc:
            raise ServiceConfigError("auto-merge project ids must be integers") from exc
    return frozenset(result)


def _nonnegative_integer(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ServiceConfigError(f"{name} must be an integer") from exc
    if parsed < 0:
        raise ServiceConfigError(f"{name} must be non-negative")
    return parsed


def _optional_int(value: str | None) -> int | None:
    if value is None or not str(value).strip():
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ServiceConfigError("bot user id must be an integer") from exc


def _optional_max_steps(value: str | None) -> int | None:
    if value is None or not str(value).strip():
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ServiceConfigError("GCA_DEFAULT_MAX_STEPS must be an integer") from exc
    if not 1 <= parsed <= 1000:
        raise ServiceConfigError("GCA_DEFAULT_MAX_STEPS must be an integer from 1 to 1000")
    return parsed


def _registrations(raw: str) -> dict[str, WebhookRegistration]:
    try:
        return parse_registrations(raw)
    except RegistrationError as exc:
        raise ServiceConfigError(str(exc)) from exc


def _membership_levels(raw: str) -> dict[tuple[int, int], int]:
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ServiceConfigError(f"membership levels must be valid JSON: {exc}") from exc
    if not isinstance(value, list):
        raise ServiceConfigError("membership levels must be a JSON array")
    result: dict[tuple[int, int], int] = {}
    for item in value:
        if not isinstance(item, Mapping):
            raise ServiceConfigError("membership level entries must be objects")
        try:
            project_id = int(item["project_id"])
            user_id = int(item["user_id"])
            access_level = int(item["access_level"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ServiceConfigError("membership level entries are invalid") from exc
        result[(project_id, user_id)] = access_level
    return result


def _secret_grants(value: str) -> dict[str, dict[str, frozenset[str]]]:
    if not value.strip():
        return {}
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ServiceConfigError(f"GCA_TOOL_SECRET_GRANTS must be valid JSON: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ServiceConfigError("GCA_TOOL_SECRET_GRANTS must be a project mapping")
    result: dict[str, dict[str, frozenset[str]]] = {}
    for project, tools in raw.items():
        if not isinstance(project, str) or not project.strip() or not isinstance(tools, Mapping):
            raise ServiceConfigError("tool secret grants require project and tool mappings")
        if "/" not in project or "://" in project:
            raise ServiceConfigError(
                "tool secret grant projects must use canonical host/group/repository keys"
            )
        project_tools: dict[str, frozenset[str]] = {}
        for tool, names in tools.items():
            if not isinstance(tool, str) or re.fullmatch(r"[a-z][a-z0-9_]*", tool) is None:
                raise ServiceConfigError(f"invalid tool secret grant name: {tool}")
            if not isinstance(names, list) or not all(
                isinstance(name, str) and name.strip() for name in names
            ):
                raise ServiceConfigError(
                    f"tool secret grant {project}.{tool} must be a list of names"
                )
            project_tools[tool] = frozenset(name.strip() for name in names)
        result[project.strip().lower()] = project_tools
    return result


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
