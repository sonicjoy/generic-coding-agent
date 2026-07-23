from __future__ import annotations

import os
import sys
from pathlib import Path

from gca.credentials import CredentialBroker
from gca.executor.fake import FakeExecutor
from gca.repo_config import CommandParameterConfig, FixedCommandConfig
from gca.tools.base import ExecutionPolicy, ToolContext
from gca.tools.fixed import FixedCommandTool


def test_fixed_command_executes_argv_and_bounded_parameter(tmp_path: Path) -> None:
    config = FixedCommandConfig(
        name="print_value",
        description="Print a bounded value.",
        argv=(sys.executable, "-c", "import sys; print(sys.argv[1])"),
        cwd=tmp_path,
        parameters={
            "value": CommandParameterConfig(
                choices=("one", "two"),
                required=True,
            )
        },
    )
    tool = FixedCommandTool(config)
    ctx = ToolContext(workspace=tmp_path, executor=FakeExecutor(execute_locally=True))

    result = tool.run(ctx, value="two")
    rejected = tool.run(ctx, value="three")

    assert result.ok and "two" in result.output
    assert not rejected.ok and "must be one of" in rejected.output


def test_fixed_command_does_not_inherit_credentials(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "super-secret-value")  # type: ignore[attr-defined]
    config = FixedCommandConfig(
        name="inspect_env",
        description="Inspect environment.",
        argv=(
            sys.executable,
            "-c",
            "import os; print(os.getenv('OPENROUTER_API_KEY', 'missing'))",
        ),
        cwd=tmp_path,
    )
    ctx = ToolContext(
        workspace=tmp_path,
        credentials=CredentialBroker.from_environment(os.environ),
        execution=ExecutionPolicy(profile="hosted"),
        executor=FakeExecutor(execute_locally=True),
    )

    result = FixedCommandTool(config).run(ctx)

    assert result.ok
    assert "missing" in result.output
    assert "super-secret-value" not in result.output


def test_fixed_command_still_obeys_core_safety_rules(tmp_path: Path) -> None:
    tool = FixedCommandTool(
        FixedCommandConfig(
            name="unsafe_delete",
            description="Must be blocked.",
            argv=("rm", "-rf", "."),
            cwd=tmp_path,
        )
    )

    result = tool.run(ToolContext(workspace=tmp_path, executor=FakeExecutor(execute_locally=True)))

    assert not result.ok
    assert "blocked by safety guardrail" in result.output


def test_fixed_command_receives_only_explicitly_scoped_secret(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "database-value")  # type: ignore[attr-defined]
    broker = CredentialBroker.from_environment(
        os.environ,
        include_names=frozenset({"DATABASE_URL"}),
    )
    tool = FixedCommandTool(
        FixedCommandConfig(
            name="check_database",
            description="Check scoped environment.",
            argv=(
                sys.executable,
                "-c",
                "import os; print('present' if os.getenv('DATABASE_URL') else 'missing')",
            ),
            cwd=tmp_path,
        )
    )
    context = ToolContext(
        workspace=tmp_path,
        credentials=broker,
        tool_secret_access={"check_database": frozenset({"DATABASE_URL"})},
        executor=FakeExecutor(execute_locally=True),
    )

    result = tool.run(context.for_tool("check_database"))

    assert result.ok
    assert "present" in result.output
