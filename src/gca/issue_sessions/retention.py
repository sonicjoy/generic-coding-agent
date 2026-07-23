"""Path-confined retention cleanup for workspaces, logs, and isolation images."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gca.executor.prune import prune_stale_run_images
from gca.issue_sessions.models import GenerationStatus, TurnStatus
from gca.issue_sessions.store import IssueSessionStore


class RetentionJanitor:
    """Delete expired turn workspaces, old structured events, and stale images."""

    def __init__(
        self,
        store: IssueSessionStore,
        *,
        workspace_root: Path,
        workspace_retention_seconds: int = 86400,
        log_retention_seconds: int = 2_592_000,
        image_retention_seconds: int | None = None,
    ) -> None:
        self.store = store
        self.workspace_root = Path(workspace_root).resolve()
        self.workspace_retention_seconds = workspace_retention_seconds
        self.log_retention_seconds = log_retention_seconds
        self.image_retention_seconds = (
            workspace_retention_seconds
            if image_retention_seconds is None
            else image_retention_seconds
        )

    def run(self) -> dict[str, int]:
        """Execute one cleanup pass."""

        deleted_workspaces = self.cleanup_workspaces()
        deleted_events = self.cleanup_events()
        deleted_images = self.cleanup_images()
        return {
            "deleted_workspaces": deleted_workspaces,
            "deleted_events": deleted_events,
            "deleted_images": deleted_images,
        }

    def cleanup_events(self) -> int:
        if self.log_retention_seconds < 0:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.log_retention_seconds)
        return self.store.purge_events_before(cutoff.isoformat())

    def cleanup_workspaces(self) -> int:
        if not self.workspace_root.exists():
            return 0
        deleted = 0
        now = datetime.now(timezone.utc)
        for path in self.workspace_root.iterdir():
            if not path.is_dir():
                continue
            resolved = path.resolve()
            if self.workspace_root not in resolved.parents and resolved != self.workspace_root:
                continue
            if not self._is_deletable(resolved, now):
                continue
            shutil.rmtree(resolved)
            deleted += 1
        return deleted

    def cleanup_images(self) -> int:
        """Remove stale per-run ``gca/<id>:run`` isolation images."""

        result = prune_stale_run_images(older_than_seconds=self.image_retention_seconds)
        return result.deleted

    def _is_deletable(self, path: Path, now: datetime) -> bool:
        marker = path / "retention.json"
        # Prefer explicit turn metadata marker written by the worker.
        if marker.is_file():
            data = json.loads(marker.read_text(encoding="utf-8"))
            status = str(data.get("status", ""))
            updated_at = str(data.get("updated_at", ""))
            if status in {
                TurnStatus.QUEUED.value,
                TurnStatus.RUNNING.value,
                TurnStatus.PAUSED_BUDGET.value,
                GenerationStatus.PUBLISHING.value,
            }:
                return False
            if not updated_at:
                return False
            try:
                stamp = datetime.fromisoformat(updated_at)
            except ValueError:
                return False
            age = (now - stamp).total_seconds()
            return age >= self.workspace_retention_seconds
        # Unknown directories under the root are left alone.
        return False
