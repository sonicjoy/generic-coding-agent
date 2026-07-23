"""Deterministic per-job workspace layout."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def normalize_run_id(value: str) -> str:
    """Normalize a session or job id into a hex workspace directory name."""

    cleaned = "".join(character for character in value.lower() if character in "0123456789abcdef")
    if not cleaned:
        raise ValueError("run id must contain hexadecimal characters")
    return cleaned


@dataclass(frozen=True)
class JobWorkspace:
    """Paths allocated to one hosted job."""

    root: Path

    @property
    def repository(self) -> Path:
        return self.root / "repo"

    @property
    def sessions(self) -> Path:
        return self.root / "sessions"

    @property
    def metadata(self) -> Path:
        return self.root / "meta"

    @classmethod
    def under(cls, workspace_root: Path, job_id: str) -> JobWorkspace:
        """Build a confined layout for a hex job identifier."""

        normalized = normalize_run_id(job_id)
        root = Path(workspace_root).resolve()
        target = (root / normalized).resolve()
        if root not in target.parents:
            raise ValueError("job workspace escapes configured root")
        return cls(root=target)

    def ensure_metadata(self) -> None:
        """Create non-repository job state directories."""

        self.sessions.mkdir(parents=True, exist_ok=True)
        self.metadata.mkdir(parents=True, exist_ok=True)
