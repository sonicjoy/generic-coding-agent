"""Command executor contract used by tools and publication checks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one command execution inside the isolation environment."""

    returncode: int
    output: str
    timed_out: bool = False


class CommandExecutor(Protocol):
    """Run target-repo commands without executing them on the harness host."""

    def run(
        self,
        *,
        argv: list[str] | None = None,
        shell_command: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str],
        timeout: int,
    ) -> CommandResult:
        """Execute argv or a shell command and return combined output."""

    def cleanup(self, *, remove_image: bool = False) -> None:
        """Remove run containers and optionally the per-run image."""
