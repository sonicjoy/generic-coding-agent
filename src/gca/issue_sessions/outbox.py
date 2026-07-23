"""Service-owned GitLab notes and trusted publication outbox processing."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from gca.credentials import CredentialBroker
from gca.integrations.git_auth import push_with_token
from gca.integrations.gitlab_events import neutralize_untrusted_markdown
from gca.integrations.http import request_bytes, request_json
from gca.integrations.scm import PublicationError, PublicationPolicy
from gca.issue_sessions.models import (
    GenerationStatus,
    OutboundAction,
    OutboundActionStatus,
    ScmLink,
)
from gca.issue_sessions.store import IssueSessionStore
from gca.jobs.models import utc_now
from gca.repo_config import RepoConfigError, load_repo_config


class GitLabApiClient(Protocol):
    """Minimal GitLab API surface used by the outbox processor."""

    def create_issue_note(
        self, *, project_id: int, issue_iid: int, body: str
    ) -> dict[str, Any]: ...

    def create_merge_request(
        self,
        *,
        project_id: int,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> dict[str, Any]: ...

    def get_merge_request(self, *, project_id: int, mr_iid: int) -> dict[str, Any]: ...

    def merge_merge_request(
        self,
        *,
        project_id: int,
        mr_iid: int,
        sha: str,
    ) -> dict[str, Any]: ...

    def retry_pipeline_job(self, *, project_id: int, job_id: int) -> dict[str, Any]: ...

    def fetch_job_trace(self, *, project_id: int, job_id: int, max_bytes: int = 20_000) -> str: ...


@dataclass
class HttpGitLabApiClient:
    """GitLab API client using a service-owned private token."""

    token: str
    api_url: str = "https://gitlab.com/api/v4"

    def _headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self.token}

    def create_issue_note(self, *, project_id: int, issue_iid: int, body: str) -> dict[str, Any]:
        result = request_json(
            "POST",
            f"{self.api_url}/projects/{project_id}/issues/{issue_iid}/notes",
            headers=self._headers(),
            body={"body": body},
        )
        if not isinstance(result, dict):
            raise RuntimeError("unexpected GitLab note response")
        return result

    def create_merge_request(
        self,
        *,
        project_id: int,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> dict[str, Any]:
        result = request_json(
            "POST",
            f"{self.api_url}/projects/{project_id}/merge_requests",
            headers=self._headers(),
            body={
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": title,
                "description": description,
            },
        )
        if not isinstance(result, dict):
            raise RuntimeError("unexpected GitLab merge request response")
        return result

    def get_merge_request(self, *, project_id: int, mr_iid: int) -> dict[str, Any]:
        result = request_json(
            "GET",
            f"{self.api_url}/projects/{project_id}/merge_requests/{mr_iid}",
            headers=self._headers(),
        )
        if not isinstance(result, dict):
            raise RuntimeError("unexpected GitLab merge request response")
        return result

    def merge_merge_request(
        self,
        *,
        project_id: int,
        mr_iid: int,
        sha: str,
    ) -> dict[str, Any]:
        result = request_json(
            "PUT",
            f"{self.api_url}/projects/{project_id}/merge_requests/{mr_iid}/merge",
            headers=self._headers(),
            body={"sha": sha, "should_remove_source_branch": False},
        )
        if not isinstance(result, dict):
            raise RuntimeError("unexpected GitLab merge response")
        return result

    def retry_pipeline_job(self, *, project_id: int, job_id: int) -> dict[str, Any]:
        result = request_json(
            "POST",
            f"{self.api_url}/projects/{project_id}/jobs/{job_id}/retry",
            headers=self._headers(),
        )
        if not isinstance(result, dict):
            raise RuntimeError("unexpected GitLab job retry response")
        return result

    def fetch_job_trace(self, *, project_id: int, job_id: int, max_bytes: int = 20_000) -> str:
        data = request_bytes(
            "GET",
            f"{self.api_url}/projects/{project_id}/jobs/{job_id}/trace",
            headers=self._headers(),
            max_bytes=max_bytes,
        )
        return data.decode("utf-8", errors="replace")


class RecordingGitLabApiClient:
    """In-memory GitLab API used by tests."""

    def __init__(self) -> None:
        self.notes: list[dict[str, Any]] = []
        self.merge_requests: list[dict[str, Any]] = []
        self.merges: list[dict[str, Any]] = []
        self._mr_counter = 1

    def create_issue_note(self, *, project_id: int, issue_iid: int, body: str) -> dict[str, Any]:
        note = {
            "id": len(self.notes) + 1,
            "project_id": project_id,
            "issue_iid": issue_iid,
            "body": body,
        }
        self.notes.append(note)
        return note

    def create_merge_request(
        self,
        *,
        project_id: int,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> dict[str, Any]:
        mr = {
            "iid": self._mr_counter,
            "id": 1000 + self._mr_counter,
            "project_id": project_id,
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
            "web_url": f"https://gitlab.example/{project_id}/-/merge_requests/{self._mr_counter}",
            "sha": "a" * 40,
        }
        self._mr_counter += 1
        self.merge_requests.append(mr)
        return mr

    def get_merge_request(self, *, project_id: int, mr_iid: int) -> dict[str, Any]:
        for item in self.merge_requests:
            if item["iid"] == mr_iid and item["project_id"] == project_id:
                return dict(item)
        raise RuntimeError("merge request not found")

    def merge_merge_request(
        self,
        *,
        project_id: int,
        mr_iid: int,
        sha: str,
    ) -> dict[str, Any]:
        payload = {"project_id": project_id, "mr_iid": mr_iid, "sha": sha, "state": "merged"}
        self.merges.append(payload)
        return payload

    def retry_pipeline_job(self, *, project_id: int, job_id: int) -> dict[str, Any]:
        return {"id": job_id + 1, "project_id": project_id, "status": "pending"}

    def fetch_job_trace(self, *, project_id: int, job_id: int, max_bytes: int = 20_000) -> str:
        return "FAILED test_example.py"


class OutboxProcessor:
    """Process durable outbound actions with at-least-once reconciliation."""

    def __init__(
        self,
        store: IssueSessionStore,
        api: GitLabApiClient,
        *,
        repository_url_resolver: Any | None = None,
        git_username: str = "oauth2",
        git_token: str = "",
        allow_auto_merge_projects: frozenset[int] = frozenset(),
    ) -> None:
        self.store = store
        self.api = api
        self.repository_url_resolver = repository_url_resolver
        self.git_username = git_username
        self.git_token = git_token
        self.allow_auto_merge_projects = allow_auto_merge_projects
        self.credentials = CredentialBroker.from_environment()

    def process_pending(self, *, limit: int = 20) -> int:
        """Process pending outbox actions; return count handled."""

        actions = self.store.list_pending_outbound(limit=limit)
        handled = 0
        for action in actions:
            self.process_one(action)
            handled += 1
        return handled

    def process_one(self, action: OutboundAction) -> None:
        with self.store.unit_of_work() as uow:
            action.status = OutboundActionStatus.LEASED
            action.attempts += 1
            uow.save_outbound_action(action)
        try:
            if action.kind == "issue_note":
                result = self._post_issue_note(action)
            elif action.kind == "publish_changes":
                result = self._publish_changes(action)
            elif action.kind == "merge_mr":
                result = self._merge_mr(action)
            else:
                raise RuntimeError(f"unsupported outbound action kind: {action.kind}")
            with self.store.unit_of_work() as uow:
                action.status = OutboundActionStatus.SUCCEEDED
                action.result = result
                action.updated_at = utc_now()
                uow.save_outbound_action(action)
                uow.append_event(
                    issue_session_id=action.issue_session_id,
                    generation_id=action.generation_id,
                    turn_id=action.turn_id,
                    kind="outbound",
                    payload={"kind": action.kind, "status": "succeeded", "result": result},
                )
        except Exception as exc:
            with self.store.unit_of_work() as uow:
                action.status = OutboundActionStatus.FAILED
                action.last_error = self.credentials.redact(str(exc))
                uow.save_outbound_action(action)
                uow.append_event(
                    issue_session_id=action.issue_session_id,
                    generation_id=action.generation_id,
                    turn_id=action.turn_id,
                    kind="outbound",
                    payload={
                        "kind": action.kind,
                        "status": "failed",
                        "error": action.last_error,
                    },
                )

    def _post_issue_note(self, action: OutboundAction) -> dict[str, Any]:
        payload = action.payload
        body = render_issue_note(payload)
        session = self.store.get_session(action.issue_session_id)
        return self.api.create_issue_note(
            project_id=session.project_id,
            issue_iid=int(payload.get("issue_iid") or session.issue_iid),
            body=body,
        )

    def _publish_changes(self, action: OutboundAction) -> dict[str, Any]:
        session = self.store.get_session(action.issue_session_id)
        generation = self.store.get_generation(action.generation_id)
        workspace = Path(str(action.payload.get("workspace_path") or ""))
        if not workspace.is_dir():
            raise RuntimeError("publish workspace is missing")
        branch = generation.branch_name or f"gca/issues/{session.issue_iid}/{generation.id[:12]}"
        marker = f"gca-ownership:{generation.id}"
        title = _safe_title(str(action.payload.get("issue_title") or session.issue_title))
        summary = neutralize_untrusted_markdown(str(action.payload.get("summary") or ""))
        commit_sha = create_trusted_commit(
            workspace,
            branch=branch,
            message=f"gca: {title}",
            credentials=self.credentials,
        )
        if self.git_token:
            push_with_token(
                workspace,
                branch,
                repository_url=session.repository_url,
                username=self.git_username,
                token=self.git_token,
            )
        description = (
            f"Automated change produced by generic-coding-agent.\n\n"
            f"Closes #{session.issue_iid}\n\n"
            f"<!-- {marker} -->\n\n"
            f"Summary:\n{summary}"
        )
        existing = self.store.get_scm_link(generation.id)
        if existing and existing.mr_iid is not None:
            mr = self.api.get_merge_request(
                project_id=session.project_id,
                mr_iid=existing.mr_iid,
            )
        else:
            mr = self.api.create_merge_request(
                project_id=session.project_id,
                source_branch=branch,
                target_branch=generation.target_branch,
                title=f"gca: {title}",
                description=description,
            )
        link = ScmLink(
            issue_session_id=session.id,
            generation_id=generation.id,
            source_project_id=session.project_id,
            target_project_id=session.project_id,
            branch_name=branch,
            target_branch=generation.target_branch,
            ownership_marker=marker,
            mr_iid=int(mr["iid"]),
            mr_global_id=str(mr.get("id", "")),
            mr_url=str(mr.get("web_url", "")),
            expected_head_sha=str(mr.get("sha") or commit_sha),
        )
        repo_auto_merge = _trusted_repo_auto_merge(workspace, generation.metadata)
        with self.store.unit_of_work() as uow:
            uow.upsert_scm_link(link)
            generation = uow.get_generation(generation.id)
            generation.status = GenerationStatus.AWAITING_MERGE
            generation.branch_name = branch
            generation.metadata = {**generation.metadata, "auto_merge": repo_auto_merge}
            uow.save_generation(generation)
            session_obj = uow.get_session(session.id)
            session_obj.status = GenerationStatus.AWAITING_MERGE
            uow.save_session(session_obj)
            uow.insert_outbound_action(
                OutboundAction(
                    issue_session_id=session.id,
                    generation_id=generation.id,
                    turn_id=action.turn_id,
                    kind="issue_note",
                    effect_key=f"note:{session.id}:{generation.id}:mr",
                    payload={
                        "template": "mr_opened",
                        "issue_iid": session.issue_iid,
                        "mr_url": link.mr_url,
                        "branch": branch,
                    },
                )
            )
            # Two-key auto-merge: operator project allowlist AND trusted repo policy.
            if session.project_id in self.allow_auto_merge_projects and repo_auto_merge:
                uow.insert_outbound_action(
                    OutboundAction(
                        issue_session_id=session.id,
                        generation_id=generation.id,
                        kind="merge_mr",
                        effect_key=f"merge:{session.id}:{generation.id}:{link.expected_head_sha}",
                        payload={
                            "project_id": session.project_id,
                            "mr_iid": link.mr_iid,
                            "sha": link.expected_head_sha,
                        },
                    )
                )
        return {
            "branch": branch,
            "commit_sha": commit_sha,
            "mr_iid": link.mr_iid,
            "mr_url": link.mr_url,
        }

    def _merge_mr(self, action: OutboundAction) -> dict[str, Any]:
        payload = action.payload
        return self.api.merge_merge_request(
            project_id=int(payload["project_id"]),
            mr_iid=int(payload["mr_iid"]),
            sha=str(payload["sha"]),
        )


def _trusted_repo_auto_merge(workspace: Path, metadata: dict[str, Any]) -> bool:
    """Resolve repository auto-merge opt-in from trusted metadata or config."""

    if metadata.get("auto_merge") is True:
        return True
    if metadata.get("auto_merge") is False:
        return False
    try:
        config = load_repo_config(workspace)
        return PublicationPolicy.from_mapping(config.publication).auto_merge
    except (RepoConfigError, PublicationError, OSError, ValueError):
        return False


def render_issue_note(payload: dict[str, Any]) -> str:
    """Render a fixed service note template with neutralized untrusted fields."""

    template = str(payload.get("template", "status"))
    marker = f"<!-- gca-effect:{payload.get('question_id') or payload.get('template')} -->"
    if template == "clarification":
        question = neutralize_untrusted_markdown(str(payload.get("question", "")))
        return f"I need a bit more information before continuing:\n\n{question}\n\n{marker}"
    if template == "ack":
        return f"Queued a coding agent session for this issue.\n\n{marker}"
    if template == "status":
        status = neutralize_untrusted_markdown(str(payload.get("status", "unknown")))
        wait = payload.get("wait_reason")
        extra = f" (waiting: {wait})" if wait else ""
        return f"Current agent status: `{status}`{extra}.\n\n{marker}"
    if template == "no_safe_change":
        summary = neutralize_untrusted_markdown(str(payload.get("summary", "")))
        return f"No safe code change was made.\n\n{summary}\n\n{marker}"
    if template == "failed":
        summary = neutralize_untrusted_markdown(str(payload.get("summary", "")))
        return f"The agent turn failed.\n\n{summary}\n\n{marker}"
    if template == "mr_opened":
        url = str(payload.get("mr_url", ""))
        branch = neutralize_untrusted_markdown(str(payload.get("branch", "")))
        return f"Opened merge request: {url}\n\nBranch: `{branch}`\n\n{marker}"
    if template == "remediation_exhausted":
        return (
            "Automatic conflict/build remediation attempts were exhausted. "
            "Please help or comment `/agent fix` after addressing blockers.\n\n"
            f"{marker}"
        )
    summary = neutralize_untrusted_markdown(str(payload.get("summary", "")))
    return f"{summary}\n\n{marker}"


def create_trusted_commit(
    workspace: Path,
    *,
    branch: str,
    message: str,
    credentials: CredentialBroker,
) -> str:
    """Create a service-owned commit from a confined candidate tree."""

    denied = {".git", ".gca/config.yaml", ".env", ".gca/.env"}
    candidates: list[Path] = []
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(workspace).as_posix()
        if (
            relative.startswith(".git/")
            or relative in denied
            or relative.startswith(".gca/sessions")
        ):
            continue
        if path.is_symlink():
            continue
        candidates.append(path)
    with tempfile.TemporaryDirectory(prefix="gca-trusted-") as temporary:
        private = Path(temporary)
        repo = private / "repo"
        repo.mkdir()
        _git(repo, ["init"], credentials)
        _git(repo, ["checkout", "-b", branch], credentials)
        for source in candidates:
            relative_path = source.relative_to(workspace)
            destination = repo / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        _git(repo, ["add", "-A"], credentials)
        _git(
            repo,
            [
                "-c",
                "user.name=Generic Coding Agent",
                "-c",
                "user.email=gca@localhost",
                "commit",
                "--allow-empty",
                "-m",
                message,
            ],
            credentials,
        )
        sha = _git(repo, ["rev-parse", "HEAD"], credentials).strip()
        # Copy commit into the agent workspace branch using clean metadata.
        bundle = private / "bundle"
        _git(repo, ["bundle", "create", str(bundle), "HEAD"], credentials)
        _git(workspace, ["fetch", str(bundle), "HEAD"], credentials)
        _git(
            workspace,
            ["checkout", "-f", "-B", branch, "FETCH_HEAD"],
            credentials,
        )
        return sha


def _git(cwd: Path, args: list[str], credentials: CredentialBroker) -> str:
    env = credentials.subprocess_env("hosted")
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    result = subprocess.run(
        ["git", "-c", "safe.directory=*", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    output = credentials.redact((result.stdout or "") + (result.stderr or ""))
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {output.strip()}")
    return output


def _safe_title(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value.strip())
    cleaned = cleaned.replace("/", "-")
    if len(cleaned) > 60:
        cleaned = cleaned[:57].rstrip() + "..."
    return cleaned or "agent change"
