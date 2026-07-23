"""SQLite unit-of-work store for durable issue sessions."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gca.issue_sessions.models import (
    GenerationStatus,
    InboundEvent,
    IssueGeneration,
    IssueSession,
    OutboundAction,
    OutboundActionStatus,
    ScmLink,
    SessionEvent,
    Turn,
)
from gca.jobs.models import Job, JobStatus, RepositorySpec, RunSpec, utc_now


class IssueSessionStoreError(RuntimeError):
    """Base error for issue-session persistence."""


class IssueSessionNotFoundError(IssueSessionStoreError):
    """Raised when an issue session ID does not exist."""


class IssueSessionConcurrencyError(IssueSessionStoreError):
    """Raised when optimistic concurrency detects a stale update."""


class DuplicateDeliveryError(IssueSessionStoreError):
    """Raised when a webhook delivery ID was already ingested."""


@dataclass
class IngestResult:
    """Outcome of durable webhook ingestion."""

    status: str
    delivery_id: str
    issue_session_id: str | None = None
    generation_id: str | None = None
    turn_id: str | None = None
    job_id: str | None = None
    event_id: str | None = None


class IssueSessionStore:
    """SQLite-backed issue session persistence sharing the jobs database."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def unit_of_work(self) -> Iterator[IssueSessionUnitOfWork]:
        """Open one transactional unit of work with immediate locking."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield IssueSessionUnitOfWork(connection)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def get_session(self, session_id: str) -> IssueSession:
        """Load one issue session by ID."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM issue_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise IssueSessionNotFoundError(f"no such issue session: {session_id}")
        return IssueSession.from_dict(json.loads(str(row["data"])))

    def find_session(
        self,
        *,
        gitlab_instance: str,
        project_id: int,
        issue_iid: int,
    ) -> IssueSession | None:
        """Look up the durable issue session for one GitLab issue."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT data FROM issue_sessions
                WHERE gitlab_instance = ? AND project_id = ? AND issue_iid = ?
                """,
                (gitlab_instance, project_id, issue_iid),
            ).fetchone()
        if row is None:
            return None
        return IssueSession.from_dict(json.loads(str(row["data"])))

    def list_sessions(
        self,
        *,
        project_id: int | None = None,
        status: GenerationStatus | None = None,
        limit: int = 50,
        after_updated_at: str | None = None,
        after_id: str | None = None,
    ) -> list[IssueSession]:
        """List issue sessions with optional filters and keyset pagination."""

        clauses: list[str] = []
        params: list[object] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if after_updated_at is not None and after_id is not None:
            clauses.append("(updated_at < ? OR (updated_at = ? AND id < ?))")
            params.extend([after_updated_at, after_updated_at, after_id])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 500)))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT data FROM issue_sessions
                {where}
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [IssueSession.from_dict(json.loads(str(row["data"]))) for row in rows]

    def get_generation(self, generation_id: str) -> IssueGeneration:
        """Load one generation by ID."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM issue_generations WHERE id = ?",
                (generation_id,),
            ).fetchone()
        if row is None:
            raise IssueSessionNotFoundError(f"no such generation: {generation_id}")
        return IssueGeneration.from_dict(json.loads(str(row["data"])))

    def get_turn(self, turn_id: str) -> Turn:
        """Load one turn by ID."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM issue_turns WHERE id = ?",
                (turn_id,),
            ).fetchone()
        if row is None:
            raise IssueSessionNotFoundError(f"no such turn: {turn_id}")
        return Turn.from_dict(json.loads(str(row["data"])))

    def list_events(
        self,
        issue_session_id: str,
        *,
        after_seq: int = 0,
        limit: int = 100,
    ) -> list[SessionEvent]:
        """Return paginated structured events for one issue session."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT data FROM session_events
                WHERE issue_session_id = ? AND seq > ?
                ORDER BY seq ASC
                LIMIT ?
                """,
                (issue_session_id, after_seq, max(1, min(limit, 500))),
            ).fetchall()
        return [SessionEvent.from_dict(json.loads(str(row["data"]))) for row in rows]

    def list_pending_outbound(self, *, limit: int = 20) -> list[OutboundAction]:
        """List pending outbox actions in creation order."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT data FROM outbound_actions
                WHERE status = ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (OutboundActionStatus.PENDING.value, max(1, min(limit, 100))),
            ).fetchall()
        return [OutboundAction.from_dict(json.loads(str(row["data"]))) for row in rows]

    def get_scm_link(self, generation_id: str) -> ScmLink | None:
        """Return the SCM ownership link for one generation if present."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM scm_links WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
        if row is None:
            return None
        return ScmLink.from_dict(json.loads(str(row["data"])))

    def find_session_by_mr(
        self,
        *,
        project_id: int,
        mr_iid: int,
    ) -> IssueSession | None:
        """Resolve an issue session from a linked merge request."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT issue_sessions.data
                FROM scm_links
                JOIN issue_sessions ON issue_sessions.id = scm_links.issue_session_id
                WHERE scm_links.target_project_id = ? AND scm_links.mr_iid = ?
                ORDER BY scm_links.updated_at DESC
                LIMIT 1
                """,
                (project_id, mr_iid),
            ).fetchone()
        if row is None:
            return None
        return IssueSession.from_dict(json.loads(str(row["data"])))

    def purge_events_before(self, cutoff_iso: str) -> int:
        """Delete structured events older than the retention cutoff."""

        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM session_events WHERE created_at < ?",
                (cutoff_iso,),
            )
            connection.commit()
            return int(cursor.rowcount)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS issue_sessions (
                    id TEXT PRIMARY KEY,
                    gitlab_instance TEXT NOT NULL,
                    project_id INTEGER NOT NULL,
                    issue_iid INTEGER NOT NULL,
                    project_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(gitlab_instance, project_id, issue_iid)
                );
                CREATE INDEX IF NOT EXISTS issue_sessions_status
                    ON issue_sessions(status, updated_at);
                CREATE INDEX IF NOT EXISTS issue_sessions_project
                    ON issue_sessions(project_id, updated_at);
                CREATE TABLE IF NOT EXISTS issue_generations (
                    id TEXT PRIMARY KEY,
                    issue_session_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(issue_session_id) REFERENCES issue_sessions(id)
                );
                CREATE INDEX IF NOT EXISTS issue_generations_session
                    ON issue_generations(issue_session_id, created_at);
                CREATE TABLE IF NOT EXISTS issue_turns (
                    id TEXT PRIMARY KEY,
                    issue_session_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    job_id TEXT,
                    data TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(issue_session_id) REFERENCES issue_sessions(id),
                    FOREIGN KEY(generation_id) REFERENCES issue_generations(id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS issue_turns_one_active
                    ON issue_turns(generation_id)
                    WHERE status IN ('queued', 'running', 'paused_budget');
                CREATE INDEX IF NOT EXISTS issue_turns_job
                    ON issue_turns(job_id);
                CREATE TABLE IF NOT EXISTS inbound_events (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    gitlab_instance TEXT NOT NULL,
                    project_id INTEGER NOT NULL,
                    delivery_id TEXT NOT NULL,
                    event_uuid TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    action TEXT NOT NULL,
                    object_key TEXT NOT NULL,
                    issue_session_id TEXT,
                    generation_id TEXT,
                    consumed_by_turn_id TEXT,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(provider, delivery_id),
                    UNIQUE(provider, project_id, object_key, action)
                );
                CREATE INDEX IF NOT EXISTS inbound_events_session
                    ON inbound_events(issue_session_id, created_at);
                CREATE TABLE IF NOT EXISTS scm_links (
                    id TEXT PRIMARY KEY,
                    issue_session_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL UNIQUE,
                    target_project_id INTEGER NOT NULL,
                    mr_iid INTEGER,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(issue_session_id) REFERENCES issue_sessions(id),
                    FOREIGN KEY(generation_id) REFERENCES issue_generations(id)
                );
                CREATE INDEX IF NOT EXISTS scm_links_mr
                    ON scm_links(target_project_id, mr_iid);
                CREATE TABLE IF NOT EXISTS outbound_actions (
                    id TEXT PRIMARY KEY,
                    issue_session_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    turn_id TEXT,
                    kind TEXT NOT NULL,
                    effect_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(issue_session_id) REFERENCES issue_sessions(id),
                    FOREIGN KEY(generation_id) REFERENCES issue_generations(id)
                );
                CREATE INDEX IF NOT EXISTS outbound_actions_status
                    ON outbound_actions(status, created_at);
                CREATE TABLE IF NOT EXISTS session_events (
                    id TEXT PRIMARY KEY,
                    issue_session_id TEXT NOT NULL,
                    generation_id TEXT,
                    turn_id TEXT,
                    seq INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(issue_session_id, seq),
                    FOREIGN KEY(issue_session_id) REFERENCES issue_sessions(id)
                );
                CREATE INDEX IF NOT EXISTS session_events_created
                    ON session_events(created_at);
                """
            )
            self._ensure_job_columns(connection)

    def _ensure_job_columns(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                idempotency_key TEXT UNIQUE,
                repository_url TEXT NOT NULL,
                status TEXT NOT NULL,
                data TEXT NOT NULL,
                version INTEGER NOT NULL,
                not_before REAL NOT NULL,
                lease_owner TEXT,
                lease_expires_at REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
        }
        alterations = {
            "issue_session_id": "ALTER TABLE jobs ADD COLUMN issue_session_id TEXT",
            "generation_id": "ALTER TABLE jobs ADD COLUMN generation_id TEXT",
            "turn_id": "ALTER TABLE jobs ADD COLUMN turn_id TEXT",
            "lease_epoch": "ALTER TABLE jobs ADD COLUMN lease_epoch INTEGER NOT NULL DEFAULT 0",
            "cancel_requested": (
                "ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"
            ),
            "project_id": "ALTER TABLE jobs ADD COLUMN project_id INTEGER",
        }
        for name, statement in alterations.items():
            if name not in columns:
                connection.execute(statement)
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS jobs_issue_session
                ON jobs(issue_session_id, status)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS jobs_project_status
                ON jobs(project_id, status)
            """
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


class IssueSessionUnitOfWork:
    """Transactional helper for webhook ingestion and turn scheduling."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def get_session(self, session_id: str) -> IssueSession:
        row = self.connection.execute(
            "SELECT data FROM issue_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise IssueSessionNotFoundError(f"no such issue session: {session_id}")
        return IssueSession.from_dict(json.loads(str(row["data"])))

    def find_session(
        self,
        *,
        gitlab_instance: str,
        project_id: int,
        issue_iid: int,
    ) -> IssueSession | None:
        row = self.connection.execute(
            """
            SELECT data FROM issue_sessions
            WHERE gitlab_instance = ? AND project_id = ? AND issue_iid = ?
            """,
            (gitlab_instance, project_id, issue_iid),
        ).fetchone()
        if row is None:
            return None
        return IssueSession.from_dict(json.loads(str(row["data"])))

    def find_delivery(self, *, provider: str, delivery_id: str) -> InboundEvent | None:
        row = self.connection.execute(
            """
            SELECT data FROM inbound_events
            WHERE provider = ? AND delivery_id = ?
            """,
            (provider, delivery_id),
        ).fetchone()
        if row is None:
            return None
        return InboundEvent.from_dict(json.loads(str(row["data"])))

    def insert_inbound_event(self, event: InboundEvent) -> InboundEvent:
        try:
            self.connection.execute(
                """
                INSERT INTO inbound_events (
                    id, provider, gitlab_instance, project_id, delivery_id, event_uuid,
                    event_type, action, object_key, issue_session_id, generation_id,
                    consumed_by_turn_id, data, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.provider,
                    event.gitlab_instance,
                    event.project_id,
                    event.delivery_id,
                    event.event_uuid,
                    event.event_type,
                    event.action,
                    event.object_key,
                    event.issue_session_id,
                    event.generation_id,
                    event.consumed_by_turn_id,
                    json.dumps(event.to_dict(), sort_keys=True),
                    event.created_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateDeliveryError(
                f"duplicate webhook delivery or object action: {event.delivery_id}"
            ) from exc
        return event

    def upsert_session(self, session: IssueSession) -> IssueSession:
        existing = self.find_session(
            gitlab_instance=session.gitlab_instance,
            project_id=session.project_id,
            issue_iid=session.issue_iid,
        )
        if existing is None:
            self.connection.execute(
                """
                INSERT INTO issue_sessions (
                    id, gitlab_instance, project_id, issue_iid, project_path, status,
                    data, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    session.gitlab_instance,
                    session.project_id,
                    session.issue_iid,
                    session.project_path,
                    session.status.value,
                    json.dumps(session.to_dict(), sort_keys=True),
                    session.version,
                    session.created_at,
                    session.updated_at,
                ),
            )
            return session
        return existing

    def save_session(self, session: IssueSession) -> IssueSession:
        expected = session.version
        session.version = expected + 1
        session.updated_at = utc_now()
        payload = json.dumps(session.to_dict(), sort_keys=True)
        cursor = self.connection.execute(
            """
            UPDATE issue_sessions
            SET status = ?, data = ?, version = ?, updated_at = ?, project_path = ?
            WHERE id = ? AND version = ?
            """,
            (
                session.status.value,
                payload,
                session.version,
                session.updated_at,
                session.project_path,
                session.id,
                expected,
            ),
        )
        if cursor.rowcount != 1:
            raise IssueSessionConcurrencyError(f"stale issue session update: {session.id}")
        return session

    def insert_generation(self, generation: IssueGeneration) -> IssueGeneration:
        self.connection.execute(
            """
            INSERT INTO issue_generations (
                id, issue_session_id, status, data, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                generation.id,
                generation.issue_session_id,
                generation.status.value,
                json.dumps(generation.to_dict(), sort_keys=True),
                generation.version,
                generation.created_at,
                generation.updated_at,
            ),
        )
        return generation

    def save_generation(self, generation: IssueGeneration) -> IssueGeneration:
        expected = generation.version
        generation.version = expected + 1
        generation.updated_at = utc_now()
        cursor = self.connection.execute(
            """
            UPDATE issue_generations
            SET status = ?, data = ?, version = ?, updated_at = ?
            WHERE id = ? AND version = ?
            """,
            (
                generation.status.value,
                json.dumps(generation.to_dict(), sort_keys=True),
                generation.version,
                generation.updated_at,
                generation.id,
                expected,
            ),
        )
        if cursor.rowcount != 1:
            raise IssueSessionConcurrencyError(f"stale generation update: {generation.id}")
        return generation

    def get_generation(self, generation_id: str) -> IssueGeneration:
        row = self.connection.execute(
            "SELECT data FROM issue_generations WHERE id = ?",
            (generation_id,),
        ).fetchone()
        if row is None:
            raise IssueSessionNotFoundError(f"no such generation: {generation_id}")
        return IssueGeneration.from_dict(json.loads(str(row["data"])))

    def insert_turn(self, turn: Turn) -> Turn:
        try:
            self.connection.execute(
                """
                INSERT INTO issue_turns (
                    id, issue_session_id, generation_id, status, job_id, data, version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn.id,
                    turn.issue_session_id,
                    turn.generation_id,
                    turn.status.value,
                    turn.job_id,
                    json.dumps(turn.to_dict(), sort_keys=True),
                    turn.version,
                    turn.created_at,
                    turn.updated_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise IssueSessionConcurrencyError(
                f"generation already has an active turn: {turn.generation_id}"
            ) from exc
        return turn

    def save_turn(self, turn: Turn) -> Turn:
        expected = turn.version
        turn.version = expected + 1
        turn.updated_at = utc_now()
        cursor = self.connection.execute(
            """
            UPDATE issue_turns
            SET status = ?, job_id = ?, data = ?, version = ?, updated_at = ?
            WHERE id = ? AND version = ?
            """,
            (
                turn.status.value,
                turn.job_id,
                json.dumps(turn.to_dict(), sort_keys=True),
                turn.version,
                turn.updated_at,
                turn.id,
                expected,
            ),
        )
        if cursor.rowcount != 1:
            raise IssueSessionConcurrencyError(f"stale turn update: {turn.id}")
        return turn

    def active_turn(self, generation_id: str) -> Turn | None:
        row = self.connection.execute(
            """
            SELECT data FROM issue_turns
            WHERE generation_id = ?
              AND status IN ('queued', 'running', 'paused_budget')
            LIMIT 1
            """,
            (generation_id,),
        ).fetchone()
        if row is None:
            return None
        return Turn.from_dict(json.loads(str(row["data"])))

    def project_has_active_coding_turn(self, project_id: int) -> bool:
        row = self.connection.execute(
            """
            SELECT 1
            FROM jobs
            WHERE project_id = ?
              AND status IN (?, ?)
            LIMIT 1
            """,
            (project_id, JobStatus.RUNNING.value, JobStatus.PUBLISHING.value),
        ).fetchone()
        return row is not None

    def create_turn_job(
        self,
        *,
        turn: Turn,
        session: IssueSession,
        generation: IssueGeneration,
        task: str,
        workflow: str | None = None,
        max_steps: int | None = None,
    ) -> Job:
        """Create a queued job row for one turn inside the same transaction."""

        # Prefer the bot-owned branch for follow-ups; otherwise frozen base SHA or target.
        checkout_ref = (
            generation.branch_name
            or generation.target_base_sha
            or generation.target_branch
            or "main"
        )
        job = Job(
            run_spec=RunSpec(
                task=task,
                repository=RepositorySpec(
                    url=session.repository_url,
                    ref=checkout_ref,
                ),
                workflow=workflow,
                max_steps=max_steps or turn.max_steps,
                labels={
                    "issue_session_id": session.id,
                    "generation_id": generation.id,
                    "turn_id": turn.id,
                    "project_id": str(session.project_id),
                    "issue_iid": str(session.issue_iid),
                    "lease_epoch": str(generation.lease_epoch),
                },
            )
        )
        payload = job.to_dict()
        payload["issue_session_id"] = session.id
        payload["generation_id"] = generation.id
        payload["turn_id"] = turn.id
        payload["lease_epoch"] = generation.lease_epoch
        payload["cancel_requested"] = generation.cancel_requested
        payload["project_id"] = session.project_id
        self.connection.execute(
            """
            INSERT INTO jobs (
                id, idempotency_key, repository_url, status, data, version, not_before,
                lease_owner, lease_expires_at, created_at, updated_at,
                issue_session_id, generation_id, turn_id, lease_epoch, cancel_requested,
                project_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.id,
                f"turn:{turn.id}",
                job.run_spec.repository.url,
                job.status.value,
                json.dumps(payload, sort_keys=True),
                job.version,
                job.not_before,
                job.lease_owner,
                job.lease_expires_at,
                job.created_at,
                job.updated_at,
                session.id,
                generation.id,
                turn.id,
                generation.lease_epoch,
                int(generation.cancel_requested),
                session.project_id,
            ),
        )
        turn.job_id = job.id
        return job

    def mark_generation_jobs_cancelled(self, generation_id: str) -> int:
        """Flag queued/running jobs for a generation as cancel-requested."""

        cursor = self.connection.execute(
            """
            UPDATE jobs
            SET cancel_requested = 1
            WHERE generation_id = ?
              AND status IN (?, ?, ?)
            """,
            (
                generation_id,
                JobStatus.QUEUED.value,
                JobStatus.RUNNING.value,
                JobStatus.PUBLISHING.value,
            ),
        )
        return int(cursor.rowcount)

    def upsert_scm_link(self, link: ScmLink) -> ScmLink:
        existing = self.connection.execute(
            "SELECT id FROM scm_links WHERE generation_id = ?",
            (link.generation_id,),
        ).fetchone()
        link.updated_at = utc_now()
        payload = json.dumps(link.to_dict(), sort_keys=True)
        if existing is None:
            self.connection.execute(
                """
                INSERT INTO scm_links (
                    id, issue_session_id, generation_id, target_project_id, mr_iid,
                    data, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    link.id,
                    link.issue_session_id,
                    link.generation_id,
                    link.target_project_id,
                    link.mr_iid,
                    payload,
                    link.created_at,
                    link.updated_at,
                ),
            )
            return link
        self.connection.execute(
            """
            UPDATE scm_links
            SET target_project_id = ?, mr_iid = ?, data = ?, updated_at = ?
            WHERE generation_id = ?
            """,
            (
                link.target_project_id,
                link.mr_iid,
                payload,
                link.updated_at,
                link.generation_id,
            ),
        )
        return link

    def insert_outbound_action(self, action: OutboundAction) -> OutboundAction:
        try:
            self.connection.execute(
                """
                INSERT INTO outbound_actions (
                    id, issue_session_id, generation_id, turn_id, kind, effect_key,
                    status, data, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action.id,
                    action.issue_session_id,
                    action.generation_id,
                    action.turn_id,
                    action.kind,
                    action.effect_key,
                    action.status.value,
                    json.dumps(action.to_dict(), sort_keys=True),
                    action.created_at,
                    action.updated_at,
                ),
            )
        except sqlite3.IntegrityError:
            row = self.connection.execute(
                "SELECT data FROM outbound_actions WHERE effect_key = ?",
                (action.effect_key,),
            ).fetchone()
            if row is None:
                raise
            return OutboundAction.from_dict(json.loads(str(row["data"])))
        return action

    def save_outbound_action(self, action: OutboundAction) -> OutboundAction:
        action.updated_at = utc_now()
        self.connection.execute(
            """
            UPDATE outbound_actions
            SET status = ?, data = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                action.status.value,
                json.dumps(action.to_dict(), sort_keys=True),
                action.updated_at,
                action.id,
            ),
        )
        return action

    def append_event(
        self,
        *,
        issue_session_id: str,
        kind: str,
        payload: dict[str, Any],
        generation_id: str | None = None,
        turn_id: str | None = None,
    ) -> SessionEvent:
        row = self.connection.execute(
            """
            SELECT COALESCE(MAX(seq), 0) AS max_seq
            FROM session_events
            WHERE issue_session_id = ?
            """,
            (issue_session_id,),
        ).fetchone()
        seq = int(row["max_seq"]) + 1 if row is not None else 1
        event = SessionEvent(
            issue_session_id=issue_session_id,
            generation_id=generation_id,
            turn_id=turn_id,
            seq=seq,
            kind=kind,
            payload=dict(payload),
        )
        self.connection.execute(
            """
            INSERT INTO session_events (
                id, issue_session_id, generation_id, turn_id, seq, kind, data, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event.issue_session_id,
                event.generation_id,
                event.turn_id,
                event.seq,
                event.kind,
                json.dumps(event.to_dict(), sort_keys=True),
                event.created_at,
            ),
        )
        return event

    def unconsumed_inbound_events(self, issue_session_id: str) -> list[InboundEvent]:
        rows = self.connection.execute(
            """
            SELECT data FROM inbound_events
            WHERE issue_session_id = ? AND consumed_by_turn_id IS NULL
            ORDER BY created_at ASC
            """,
            (issue_session_id,),
        ).fetchall()
        return [InboundEvent.from_dict(json.loads(str(row["data"]))) for row in rows]

    def mark_events_consumed(self, event_ids: list[str], turn_id: str) -> None:
        for event_id in event_ids:
            row = self.connection.execute(
                "SELECT data FROM inbound_events WHERE id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                continue
            event = InboundEvent.from_dict(json.loads(str(row["data"])))
            event.consumed_by_turn_id = turn_id
            self.connection.execute(
                """
                UPDATE inbound_events
                SET consumed_by_turn_id = ?, data = ?
                WHERE id = ?
                """,
                (turn_id, json.dumps(event.to_dict(), sort_keys=True), event_id),
            )

    def bump_lease_epoch(self, generation: IssueGeneration) -> IssueGeneration:
        generation.lease_epoch += 1
        return self.save_generation(generation)

    def now(self) -> float:
        return time.time()
