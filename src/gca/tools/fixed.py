"""Manifest-defined commands executed without a shell."""

from __future__ import annotations

import shlex
import subprocess
from typing import Any

from gca.repo_config import CommandParameterConfig, FixedCommandConfig
from gca.tools.base import Tool, ToolContext, ToolResult


class FixedCommandTool(Tool):
    """Execute one fixed argv command with bounded, validated arguments."""

    capabilities = frozenset({"execute"})
    risk = "medium"

    def __init__(self, config: FixedCommandConfig) -> None:
        self.config = config
        self.name = config.name
        self.description = config.description
        self.parameters = _parameter_schema(config.parameters)

    def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        unknown = sorted(set(kwargs) - set(self.config.parameters))
        if unknown:
            return ToolResult.failure(f"unknown command parameters: {', '.join(unknown)}")
        argv = list(self.config.argv)
        for name, parameter in self.config.parameters.items():
            if name not in kwargs:
                if parameter.required:
                    return ToolResult.failure(f"missing required command parameter: {name}")
                continue
            error = _append_parameter(argv, name, parameter, kwargs[name])
            if error is not None:
                return ToolResult.failure(error)

        timeout = min(self.config.timeout, ctx.execution.max_tool_timeout)
        try:
            proc = subprocess.run(
                argv,
                shell=False,
                cwd=str(self.config.cwd),
                env=ctx.subprocess_env(),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ToolResult.failure(f"command timed out after {timeout}s: {shlex.join(argv)}")
        except OSError as exc:
            return ToolResult.failure(f"could not execute {argv[0]!r}: {exc}")

        output = (proc.stdout or "") + (proc.stderr or "")
        output = ctx.redact(output)
        if len(output) > ctx.execution.max_output_chars:
            output = output[: ctx.execution.max_output_chars] + "\n... (output truncated)"
        result = f"$ {shlex.join(argv)}\n(exit code: {proc.returncode})\n{output}"
        return ToolResult.success(result) if proc.returncode == 0 else ToolResult.failure(result)


def _parameter_schema(parameters: dict[str, CommandParameterConfig]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, parameter in parameters.items():
        schema: dict[str, Any] = {"type": parameter.type}
        if parameter.choices:
            schema["enum"] = list(parameter.choices)
        properties[name] = schema
        if parameter.required:
            required.append(name)
    result: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    return result


def _append_parameter(
    argv: list[str],
    name: str,
    config: CommandParameterConfig,
    value: object,
) -> str | None:
    if config.type == "boolean":
        if not isinstance(value, bool):
            return f"command parameter {name} must be a boolean"
        if value:
            if config.flag is None:
                return f"boolean command parameter {name} requires a configured flag"
            argv.append(config.flag)
        return None
    if config.type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"command parameter {name} must be an integer"
    elif not isinstance(value, str):
        return f"command parameter {name} must be a string"
    rendered = str(value)
    if config.choices and rendered not in config.choices:
        return f"command parameter {name} must be one of: {', '.join(config.choices)}"
    if config.flag is not None:
        argv.append(config.flag)
    argv.append(rendered)
    return None
