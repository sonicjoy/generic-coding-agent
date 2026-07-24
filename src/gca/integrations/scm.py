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
from gca.tools.python_source import validate_python_source

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

    def link_branch_to_issue(
        self,
        repository_url: str,
        branch: str,
        issue_id: str,
        oid: str,
    ) -> bool: ...


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
        open_change_requests: bool = True,
    ) -> None:
        self.adapters = dict(adapters)
        self.credentials = credentials or CredentialBroker.from_environment()
        self.git_user_name = git_user_name
        self.git_user_email = git_user_email
        self.tool_secret_grants = {
            project.lower(): dict(tools) for project, tools in (tool_secret_grants or {}).items()
        }
        self.open_change_requests = open_change_requests

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
        base_ref = _checkout_publication_branch(
            workspace,
            branch,
            target.base_ref,
            self.credentials,
        )
        _git(workspace, ["add", "-A"], self.credentials)
        changed_files, changed_lines = _staged_diff(workspace, self.credentials)
        if changed_files:
            _enforce_diff(policy, changed_files, changed_lines)
            message = _commit_message(policy.commit_prefix, job)
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
        if not self.open_change_requests:
            return PublicationResult(
                branch=branch,
                commit_sha=sha,
                change_request_url=None,
            ).to_dict()
        title = _commit_message(policy.commit_prefix, job)
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

    def prepare_working_branch(self, job: Job, workspace: Path) -> dict[str, object] | None:
        """Create and push the issue-linked working branch before the agent runs."""

        target = job.run_spec.publication
        labels = job.run_spec.labels
        issue_id = str(labels.get("issue_id", "")).strip()
        # Only labeled-issue jobs (not PR review /agent fix runs that also carry
        # an issue/PR number under issue_id).
        if target is None or not issue_id or labels.get("source") != "issues.labeled":
            return None
        adapter = self.adapters.get(target.provider)
        if adapter is None:
            raise PublicationError(f"no SCM adapter configured for provider: {target.provider}")
        if not adapter.supports_repository(job.run_spec.repository.url):
            raise PublicationError(f"{target.provider} adapter does not match repository host")
        branch = _branch_name(target.branch_prefix, job.id)
        # Create from the already-cloned HEAD; do not re-fetch publication.base_ref
        # (shallow PR-head clones may not have that ref yet).
        _git(workspace, ["check-ref-format", "--branch", branch], self.credentials)
        if _git_ok(
            workspace,
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            self.credentials,
        ):
            _git(workspace, ["checkout", branch], self.credentials)
        else:
            _git(workspace, ["checkout", "-b", branch], self.credentials)
        oid = _git(workspace, ["rev-parse", "HEAD"], self.credentials).strip()
        linked = False
        try:
            linked = adapter.link_branch_to_issue(
                job.run_spec.repository.url,
                branch,
                issue_id,
                oid,
            )
        except Exception:
            linked = False
        adapter.push(workspace, branch, job.run_spec.repository.url)
        return {
            "branch": branch,
            "commit_sha": oid,
            "linked_issue": linked,
        }

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
        # Always parse-check changed Python before any repo-defined checks or push.
        _run_python_syntax_gate(workspace, self.credentials)
        if not policy.required_checks:
            _run_default_quality_gates(
                workspace,
                self.credentials,
                repo_config,
                executor=executor,
            )
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


def _changed_python_paths(workspace: Path, credentials: CredentialBroker) -> list[str]:
    """Return workspace-relative ``.py`` paths changed since ``HEAD``."""

    tracked = _git(
        workspace,
        ["diff", "--name-only", "--diff-filter=ACMR", "HEAD"],
        credentials,
    )
    untracked = _git(
        workspace,
        ["ls-files", "--others", "--exclude-standard"],
        credentials,
    )
    paths: list[str] = []
    for line in f"{tracked}\n{untracked}".splitlines():
        relative = line.strip()
        if not relative.endswith(".py"):
            continue
        if (workspace / relative).is_file():
            paths.append(relative)
    return sorted(set(paths))


def _run_python_syntax_gate(workspace: Path, credentials: CredentialBroker) -> None:
    """Fail publication when changed Python files do not parse.

    Runs in-process against the workspace checkout so it does not depend on
    Python being installed inside the isolation image (the packaged default
    image is shell/git only).
    """

    for relative in _changed_python_paths(workspace, credentials):
        content = (workspace / relative).read_text(encoding="utf-8")
        error = validate_python_source(relative, content)
        if error is not None:
            raise PublicationError(f"publication quality gate failed: {error}")


def _run_default_quality_gates(
    workspace: Path,
    credentials: CredentialBroker,
    repo_config: RepoConfig,
    *,
    executor: CommandExecutor | None,
) -> None:
    """Run lint/type quality gates when the repo did not configure required_checks.

    Attempts ``ruff check`` and ``python -m mypy`` on changed ``.py`` files through
    the isolation executor. Missing tools are skipped; installed tools that fail
    block publication.
    """

    paths = _changed_python_paths(workspace, credentials)
    if not paths or executor is None:
        return
    timeout = min(120, repo_config.runtime.max_tool_timeout)
    env = credentials.subprocess_env("hosted")
    gates = (
        ("ruff", ["ruff", "check", *paths]),
        ("mypy", ["python", "-m", "mypy", "--follow-imports=skip", *paths]),
    )
    for name, argv in gates:
        result = executor.run(argv=argv, cwd=workspace, env=env, timeout=timeout)
        output = credentials.redact(result.output)
        if result.timed_out:
            raise PublicationError(f"publication quality gate {name!r} timed out:\n{output}")
        if _quality_tool_unavailable(result.returncode, output):
            continue
        if result.returncode != 0:
            raise PublicationError(
                f"publication quality gate {name!r} failed "
                f"($ {shlex.join(argv)}, exit {result.returncode}):\n{output.strip()}"
            )


def _quality_tool_unavailable(returncode: int, output: str) -> bool:
    """Return True when a quality-gate binary/module is not installed."""

    if returncode == 127:
        return True
    lowered = output.lower()
    markers = (
        "not found",
        "no such file",
        "no module named",
        "cannot find the file",
        "is not recognized",
        "unable to locate",
    )
    return any(marker in lowered for marker in markers)


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
    """Resolve a local or remote base ref, fetching from origin when needed.

    Shallow ``--single-branch`` clones of a PR head omit ``main``/base locally;
    publication must fetch that base before opening a change request.
    """

    if not base.strip() or base.startswith("-") or ".." in base or "\x00" in base:
        raise PublicationError(f"publication base ref is invalid: {base}")
    if _git_ok(workspace, ["show-ref", "--verify", "--quiet", f"refs/heads/{base}"], credentials):
        return base
    remote = f"origin/{base}"
    if _git_ok(
        workspace,
        ["show-ref", "--verify", "--quiet", f"refs/remotes/{remote}"],
        credentials,
    ):
        return remote
    # Fetch only the requested base into a remote-tracking ref (no checkout).
    _git(
        workspace,
        [
            "fetch",
            "--depth",
            "1",
            "origin",
            f"+refs/heads/{base}:refs/remotes/origin/{base}",
        ],
        credentials,
    )
    if _git_ok(
        workspace,
        ["show-ref", "--verify", "--quiet", f"refs/remotes/{remote}"],
        credentials,
    ):
        return remote
    raise PublicationError(f"publication base ref does not exist: {base}")


def _checkout_publication_branch(
    workspace: Path,
    branch: str,
    base: str,
    credentials: CredentialBroker,
) -> str:
    _git(workspace, ["check-ref-format", "--branch", branch], credentials)
    base_ref = _existing_base_ref(workspace, base, credentials)
    branch_exists = _git_ok(
        workspace,
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        credentials,
    )
    _git(
        workspace,
        ["checkout", branch] if branch_exists else ["checkout", "-b", branch],
        credentials,
    )
    return base_ref


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


def _commit_message(prefix: str, job: Job) -> str:
    summary = _publication_summary(job)
    if len(summary) > 72:
        summary = summary[:69].rstrip() + "..."
    return f"{prefix}: {summary}"


def _publication_summary(job: Job) -> str:
    """Prefer the originating issue title over the SCM framing preamble."""

    labels = job.run_spec.labels
    issue_title = str(labels.get("issue_title", "")).strip()
    if issue_title:
        return " ".join(issue_title.split())

    task = job.run_spec.task.strip()
    for line in task.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("title:"):
            extracted = stripped.split(":", 1)[1].strip()
            if extracted:
                return " ".join(extracted.split())

    for line in task.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith("scm issue task"):
            continue
        return " ".join(stripped.split())
    return "automated change"


def _change_request_body(job: Job) -> str:
    labels = job.run_spec.labels
    issue_id = str(labels.get("issue_id", "")).strip()
    provider = str(labels.get("provider", "")).strip().lower()
    lines: list[str] = []
    if issue_id:
        # GitHub closing keyword; GitLab also recognizes Fixes/Closes.
        keyword = "Closes" if provider == "gitlab" else "Fixes"
        lines.extend([f"{keyword} #{issue_id}", ""])
    lines.extend(
        [
            "Automated change produced by generic-coding-agent.",
            "",
            f"Job: `{job.id}`",
            f"Task: {job.run_spec.task.strip()}",
        ]
    )
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
