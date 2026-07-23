"""In-memory executor used by offline unit tests."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from gca.executor.protocol import CommandResult


@dataclass
class FakeCall:
    """One recorded fake executor invocation."""

    argv: list[str] | None
    shell_command: str | None
    cwd: Path | None
    env: dict[str, str]
    timeout: int


@dataclass
class FakeExecutor:
    """Record command calls and return scripted or local subprocess results."""

    results: list[CommandResult] = field(default_factory=list)
    calls: list[FakeCall] = field(default_factory=list)
    cleaned_up: bool = False
    remove_image_requested: bool = False
    execute_locally: bool = False
    default_result: CommandResult = field(
        default_factory=lambda: CommandResult(returncode=0, output="ok\n")
    )

    def run(
        self,
        *,
        argv: list[str] | None = None,
        shell_command: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str],
        timeout: int,
    ) -> CommandResult:
        """Record the call and return the next scripted or local result."""

        if (argv is None) == (shell_command is None):
            raise ValueError("provide exactly one of argv or shell_command")
        self.calls.append(
            FakeCall(
                argv=list(argv) if argv is not None else None,
                shell_command=shell_command,
                cwd=Path(cwd) if cwd is not None else None,
                env=dict(env),
                timeout=timeout,
            )
        )
        if self.results:
            return self.results.pop(0)
        if self.execute_locally:
            return self._run_local(
                argv=argv,
                shell_command=shell_command,
                cwd=cwd,
                env=env,
                timeout=timeout,
            )
        return self.default_result

    def cleanup(self, *, remove_image: bool = False) -> None:
        """Mark cleanup as requested for assertions."""

        self.cleaned_up = True
        self.remove_image_requested = remove_image

    def _run_local(
        self,
        *,
        argv: list[str] | None,
        shell_command: str | None,
        cwd: Path | None,
        env: Mapping[str, str],
        timeout: int,
    ) -> CommandResult:
        try:
            completed = subprocess.run(
                shell_command if shell_command is not None else list(argv or []),
                shell=shell_command is not None,
                cwd=str(cwd) if cwd is not None else None,
                env=dict(env),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            rendered = shell_command or " ".join(argv or [])
            return CommandResult(
                returncode=124,
                output=f"command timed out after {timeout}s: {rendered}",
                timed_out=True,
            )
        return CommandResult(
            returncode=completed.returncode,
            output=(completed.stdout or "") + (completed.stderr or ""),
            timed_out=False,
        )
