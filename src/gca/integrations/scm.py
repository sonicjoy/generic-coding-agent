"""Service-owned Git publication and provider adapter contracts."""

from __future__ import annotations

import fnmatch
import re
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from gca.credentials import CredentialBroker
from gca.executor.protocol import CommandExecutor
from gca.integrations.repository import repository_identity
from gca.jobs.models import Job
from gca.repo_config import RepoConfig
from gca.tools.base import ExecutionPolicy, ToolContext
from gca.tools.fixed import FixedCommandTool

_CORE_DENIED_PATHS = (
    ".gca/config.yaml",
    ".gca/sessions/**",
    ".env",
    ".gca/.env",
)


class PublicationError(RuntimeError):
    """Raised when a completed job cannot be safely published."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class PublicationPolicy:
    """Repository-owned limits enforced before service publication."""

    required_checks: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    denied_paths: tuple[str, ...] = (
        ".gca/config.yaml",
        ".gca/sessions/**",
        ".env",
        ".gca/.env",
    )
    max_files: int = 100
    max_changed_lines: int = 5000
    commit_prefix: str = "gca"
    auto_merge: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> PublicationPolicy:
        """Validate a repository publication mapping."""

        raw = dict(value or {})
        allowed = {
            "required_checks",
            "allowed_paths",
            "denied_paths",
            "max_files",
            "max_changed_lines",
            "commit_prefix",
            "auto_merge",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise PublicationError(f"unknown publication keys: {', '.join(unknown)}")
        auto_merge = raw.get("auto_merge", False)
        if not isinstance(auto_merge, bool):
            raise PublicationError("auto_merge must be a boolean")
        return cls(
            required_checks=_strings(raw.get("required_checks", []), "required_checks"),
            allowed_paths=_strings(raw.get("allowed_paths", []), "allowed_paths"),
            denied_paths=_strings(
                raw.get(
                    "denied_paths",
                    [".gca/config.yaml", ".gca/sessions/**", ".env", ".gca/.env"],
                ),
                "denied_paths",
            ),
            max_files=_positive_int(raw.get("max_files", 100), "max_files"),
            max_changed_lines=_positive_int(
                raw.get("max_changed_lines", 5000),
                "max_changed_lines",
            ),
            commit_prefix=_nonempty(raw.get("commit_prefix", "gca"), "commit_prefix"),
            auto_merge=auto_merge,
        )


@dataclass(frozen=True)
class ChangeRequest:
    """Provider-independent change-request payload."""

    repository_url: str
    source_branch: str
    target_branch: str
    title: str
    body: str
    draft: bool
    commit_sha: str


@dataclass(frozen=True)
class PublicationResult:
    """Durable publication outcome."""

    branch: str
    commit_sha: str
    change_request_url: str | None
    no_changes: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "change_request_url": self.change_request_url,
            "no_changes": self.no_changes,
        }


class ScmAdapter(Protocol):
    """Vendor adapter used only by the service-owned publisher."""

    provider: str

    def supports_repository(self, repository_url: str) -> bool: ...

    def push(self, workspace: Path, branch: str, repository_url: str) -> None: ...

    def open_change_request(self, request: ChangeRequest) -> str: ...


class PublicationController:
    """Validate, commit, push, and open a change request after agent success."""

    def __init__(
        self,
        adapters: Mapping[str, ScmAdapter],
        *,
        credentials: CredentialBroker | None = None,
        git_user_name: str = "Generic Coding Agent",
        git_user_email: str = "gca@localhost",
        tool_secret_grants: Mapping[str, Mapping[str, frozenset[str]]] | None = None,
    ) -> None:
        self.adapters = dict(adapters)
        self.credentials = credentials or CredentialBroker.from_environment()
        self.git_user_name = git_user_name
        self.git_user_email = git_user_email
        self.tool_secret_grants = {
            project.lower(): dict(tools) for project, tools in (tool_secret_grants or {}).items()
        }

    def publish(
        self,
        job: Job,
        workspace: Path,
        repo_config: RepoConfig,
        *,
        executor: CommandExecutor | None = None,
    ) -> dict[str, object]:
        """Publish one completed job through its configured SCM target."""

        target = job.run_spec.publication
        if target is None:
            raise PublicationError("job has no publication target")
        adapter = self.adapters.get(target.provider)
        if adapter is None:
            raise PublicationError(f"no SCM adapter configured for provider: {target.provider}")
        if not adapter.supports_repository(job.run_spec.repository.url):
            raise PublicationError(f"{target.provider} adapter does not match repository host")
        policy = PublicationPolicy.from_mapping(repo_config.publication)
        self._run_required_checks(job, workspace, repo_config, policy, executor=executor)

        branch = _branch_name(target.branch_prefix, job.id)
        _git(workspace, ["check-ref-format", "--branch", branch], self.credentials)
        base_ref = _existing_base_ref(workspace, target.base_ref, self.credentials)
        branch_exists = _git_ok(
            workspace,
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            self.credentials,
        )
        _git(
            workspace,
            ["checkout", branch] if branch_exists else ["checkout", "-b", branch],
            self.credentials,
        )
        _git(workspace, ["add", "-A"], self.credentials)
        changed_files, changed_lines = _staged_diff(workspace, self.credentials)
        if changed_files:
            _enforce_diff(policy, changed_files, changed_lines)
            message = _commit_message(policy.commit_prefix, job.run_spec.task)
            _git(
                workspace,
                [
                    "-c",
                    f"user.name={self.git_user_name}",
                    "-c",
                    f"user.email={self.git_user_email}",
                    "commit",
                    "-m",
                    message,
                ],
                self.credentials,
            )
        else:
            commits_ahead = _commits_ahead(workspace, base_ref, self.credentials)
            if commits_ahead == 0:
                sha = _git(workspace, ["rev-parse", "HEAD"], self.credentials).strip()
                return PublicationResult(
                    branch=branch,
                    commit_sha=sha,
                    change_request_url=None,
                    no_changes=True,
                ).to_dict()
            committed_files, committed_lines = _range_diff(
                workspace,
                f"{base_ref}..HEAD",
                self.credentials,
            )
            _enforce_diff(policy, committed_files, committed_lines)

        sha = _git(workspace, ["rev-parse", "HEAD"], self.credentials).strip()
        adapter.push(workspace, branch, job.run_spec.repository.url)
        title = _commit_message(policy.commit_prefix, job.run_spec.task)
        request = ChangeRequest(
            repository_url=job.run_spec.repository.url,
            source_branch=branch,
            target_branch=target.base_ref,
            title=title,
            body=_change_request_body(job),
            draft=target.draft,
            commit_sha=sha,
        )
        url = adapter.open_change_request(request)
        return PublicationResult(
            branch=branch,
            commit_sha=sha,
            change_request_url=url,
        ).to_dict()

    def _run_required_checks(
        self,
        job: Job,
        workspace: Path,
        repo_config: RepoConfig,
        policy: PublicationPolicy,
        *,
        executor: CommandExecutor | None = None,
    ) -> None:
        try:
            project = repository_identity(job.run_spec.repository.url)
        except ValueError:
            project = ""
        grants = self.tool_secret_grants.get(project, {})
        unauthorized = {
            tool: sorted(secrets - grants.get(tool, frozenset()))
            for tool, secrets in repo_config.tools.secret_access.items()
        }
        unauthorized = {tool: secrets for tool, secrets in unauthorized.items() if secrets}
        if unauthorized:
            details = "; ".join(
                f"{tool}={','.join(names)}" for tool, names in sorted(unauthorized.items())
            )
            raise PublicationError(
                f"repository requested unapproved publication secret grants: {details}"
            )
        configured_names = frozenset(
            secret for secrets in repo_config.tools.secret_access.values() for secret in secrets
        )
        configured_broker = CredentialBroker.from_environment(include_names=configured_names)
        broker = CredentialBroker({**self.credentials.secrets, **configured_broker.secrets})
        for name in policy.required_checks:
            command = repo_config.tools.fixed_commands.get(name)
            if command is None:
                raise PublicationError(f"required check is not a fixed command: {name}")
            context = ToolContext(
                workspace=workspace,
                phase="publication",
                audit_id="publication",
                allowed_tools=frozenset({name}),
                tool_secret_access=repo_config.tools.secret_access,
                execution=ExecutionPolicy(
                    profile="hosted",
                    max_tool_timeout=repo_config.runtime.max_tool_timeout,
                    max_output_chars=repo_config.runtime.max_output_chars,
                    max_read_bytes=repo_config.runtime.max_read_bytes,
                ),
                credentials=broker,
                executor=executor,
            )
            result = FixedCommandTool(command).run(context.for_tool(name))
            if not result.ok:
                raise PublicationError(f"required check {name!r} failed:\n{result.output}")


def _git(
    workspace: Path,
    args: list[str],
    credentials: CredentialBroker,
) -> str:
    result = subprocess.run(
        ["git", *args],
        shell=False,
        cwd=workspace,
        env=credentials.subprocess_env("hosted"),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    output = credentials.redact((result.stdout or "") + (result.stderr or ""))
    if result.returncode != 0:
        raise PublicationError(f"$ git {shlex.join(args)}\n{output.strip()}")
    return output


def _git_ok(
    workspace: Path,
    args: list[str],
    credentials: CredentialBroker,
) -> bool:
    result = subprocess.run(
        ["git", *args],
        shell=False,
        cwd=workspace,
        env=credentials.subprocess_env("hosted"),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.returncode == 0


def _existing_base_ref(
    workspace: Path,
    base: str,
    credentials: CredentialBroker,
) -> str:
    if _git_ok(workspace, ["show-ref", "--verify", "--quiet", f"refs/heads/{base}"], credentials):
        return base
    remote = f"origin/{base}"
    if _git_ok(
        workspace,
        ["show-ref", "--verify", "--quiet", f"refs/remotes/{remote}"],
        credentials,
    ):
        return remote
    raise PublicationError(f"publication base ref does not exist: {base}")


def _staged_diff(
    workspace: Path,
    credentials: CredentialBroker,
) -> tuple[list[str], int]:
    names = _git(workspace, ["diff", "--cached", "--name-only", "-z"], credentials)
    files = [name for name in names.split("\0") if name]
    numstat = _git(workspace, ["diff", "--cached", "--numstat"], credentials)
    changed_lines = 0
    for line in numstat.splitlines():
        parts = line.split("\t", 2)
        if len(parts) >= 2:
            changed_lines += sum(int(value) if value.isdigit() else 1 for value in parts[:2])
    return files, changed_lines


def _range_diff(
    workspace: Path,
    revision_range: str,
    credentials: CredentialBroker,
) -> tuple[list[str], int]:
    names = _git(
        workspace,
        ["diff", "--name-only", "-z", revision_range],
        credentials,
    )
    files = [name for name in names.split("\0") if name]
    numstat = _git(
        workspace,
        ["diff", "--numstat", revision_range],
        credentials,
    )
    changed_lines = 0
    for line in numstat.splitlines():
        parts = line.split("\t", 2)
        if len(parts) >= 2:
            changed_lines += sum(int(value) if value.isdigit() else 1 for value in parts[:2])
    return files, changed_lines


def _enforce_diff(
    policy: PublicationPolicy,
    files: list[str],
    changed_lines: int,
) -> None:
    if len(files) > policy.max_files:
        raise PublicationError(
            f"diff contains {len(files)} files, exceeding limit {policy.max_files}"
        )
    if changed_lines > policy.max_changed_lines:
        raise PublicationError(
            f"diff contains {changed_lines} changed lines, "
            f"exceeding limit {policy.max_changed_lines}"
        )
    for path in files:
        if any(fnmatch.fnmatch(path, pattern) for pattern in _CORE_DENIED_PATHS):
            raise PublicationError(f"diff contains protected path: {path}")
        if policy.denied_paths and any(
            fnmatch.fnmatch(path, pattern) for pattern in policy.denied_paths
        ):
            raise PublicationError(f"diff contains denied path: {path}")
        if policy.allowed_paths and not any(
            fnmatch.fnmatch(path, pattern) for pattern in policy.allowed_paths
        ):
            raise PublicationError(f"diff path is not allowed: {path}")


def _commits_ahead(
    workspace: Path,
    base_ref: str,
    credentials: CredentialBroker,
) -> int:
    value = _git(
        workspace,
        ["rev-list", "--count", f"{base_ref}..HEAD"],
        credentials,
    ).strip()
    return int(value)


def _branch_name(prefix: str, job_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._/-]+", "-", prefix).strip("/")
    branch = f"{normalized}/{job_id[:12]}" if normalized else f"gca/{job_id[:12]}"
    if branch.startswith("-") or ".." in branch or branch.endswith(".lock"):
        raise PublicationError(f"invalid generated branch name: {branch}")
    return branch


def _commit_message(prefix: str, task: str) -> str:
    summary = " ".join(task.strip().splitlines()[0].split())
    if len(summary) > 60:
        summary = summary[:57].rstrip() + "..."
    return f"{prefix}: {summary}"


def _change_request_body(job: Job) -> str:
    lines = [
        "Automated change produced by generic-coding-agent.",
        "",
        f"Job: `{job.id}`",
        f"Task: {job.run_spec.task.strip()}",
    ]
    if job.session_id:
        lines.append(f"Session: `{job.session_id}`")
    return "\n".join(lines)


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise PublicationError(f"{label} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PublicationError(f"{label} must be a positive integer")
    return value


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PublicationError(f"{label} must be a non-empty string")
    return value.strip()
