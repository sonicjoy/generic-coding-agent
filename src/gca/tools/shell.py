"""Built-in command-execution tool.

Runs a shell command inside the workspace with a timeout, capturing combined
stdout/stderr and the exit code. This backs the agent's ability to run tests,
linters, formatters, build commands, dev servers, and static-analysis tools.

Destructive commands (``rm``, forced git rewrites, ``sudo``, etc.) are rejected
before execution by :mod:`gca.tools.safety`.
"""

from __future__ import annotations

import subprocess
from typing import Any

from gca.tools.base import Tool, ToolContext, ToolResult
from gca.tools.safety import check_command

_DEFAULT_TIMEOUT = 120
_MAX_OUTPUT = 20_000


class RunCommandTool(Tool):
    """Execute a shell command within the workspace and capture its output."""

    name = "run_command"
    description = (
        "Run a shell command in the workspace and return its combined stdout/stderr "
        "and exit code. Use for tests, linters, formatters, builds, and analysis tools. "
        "Destructive commands are blocked (rm/rmdir/unlink, sudo, git push --force, "
        "git reset --hard, git clean -f, and similar). Prefer delete_file and "
        "apply_patch for intentional file changes."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Command line to execute."},
            "timeout": {
                "type": "integer",
                "description": f"Timeout in seconds (default {_DEFAULT_TIMEOUT}).",
            },
        },
        "required": ["command"],
    }

    def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        command = str(kwargs["command"])
        blocked = check_command(command)
        if blocked is not None:
            return ToolResult.failure(
                f"blocked by safety guardrail ({blocked.rule}): {blocked.reason}"
            )
        timeout = int(kwargs.get("timeout", _DEFAULT_TIMEOUT))
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(ctx.workspace),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ToolResult.failure(f"command timed out after {timeout}s: {command}")

        output = (proc.stdout or "") + (proc.stderr or "")
        if len(output) > _MAX_OUTPUT:
            output = output[:_MAX_OUTPUT] + "\n... (output truncated)"
        header = f"$ {command}\n(exit code: {proc.returncode})\n"
        result = header + output
        if proc.returncode == 0:
            return ToolResult.success(result)
        return ToolResult.failure(result)


def shell_tools() -> list[Tool]:
    return [RunCommandTool()]
