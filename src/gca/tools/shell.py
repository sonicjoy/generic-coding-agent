"""Built-in command-execution tool.

Runs a shell command inside the workspace isolation container with a timeout,
capturing combined stdout/stderr and the exit code. This backs the agent's
ability to run tests, linters, formatters, builds, and static-analysis tools.

Destructive commands (``rm``, forced git rewrites, ``sudo``, etc.) are rejected
before execution by :mod:`gca.tools.safety`.
"""

from __future__ import annotations

from typing import Any

from gca.tools.base import Tool, ToolContext, ToolError, ToolResult
from gca.tools.safety import check_command

_DEFAULT_TIMEOUT = 120
_MAX_OUTPUT = 20_000


class RunCommandTool(Tool):
    """Execute a shell command within the workspace and capture its output."""

    name = "run_command"
    capabilities = frozenset({"execute"})
    risk = "high"
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
        blocked = check_command(command, hosted=ctx.execution.profile == "hosted")
        if blocked is not None:
            return ToolResult.failure(
                f"blocked by safety guardrail ({blocked.rule}): {blocked.reason}"
            )
        if ctx.executor is None:
            raise ToolError("command executor is not configured for this run")
        requested_timeout = int(kwargs.get("timeout", _DEFAULT_TIMEOUT))
        timeout = max(1, min(requested_timeout, ctx.execution.max_tool_timeout))
        result = ctx.executor.run(
            shell_command=command,
            cwd=ctx.workspace,
            env=ctx.subprocess_env(),
            timeout=timeout,
        )
        if result.timed_out:
            return ToolResult.failure(result.output)

        output = ctx.redact(result.output)
        output_limit = min(_MAX_OUTPUT, ctx.execution.max_output_chars)
        if len(output) > output_limit:
            output = output[:output_limit] + "\n... (output truncated)"
        header = f"$ {command}\n(exit code: {result.returncode})\n"
        rendered = header + output
        if result.returncode == 0:
            return ToolResult.success(rendered)
        return ToolResult.failure(rendered)


def shell_tools() -> list[Tool]:
    return [RunCommandTool()]


__all__ = ["RunCommandTool", "shell_tools"]
