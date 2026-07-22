from __future__ import annotations

from pathlib import Path

import pytest

from gca.tools.base import ToolContext
from gca.tools.safety import check_command
from gca.tools.shell import RunCommandTool


@pytest.mark.parametrize(
    ("command", "rule"),
    [
        ("rm -rf .", "rm"),
        ("rmdir tmp", "rm"),
        ("/bin/rm notes.txt", "rm"),
        ("echo hi && rm file.txt", "rm"),
        ("unlink path.txt", "unlink"),
        ("sudo apt-get update", "sudo"),
        ("git push --force origin main", "git-push-force"),
        ("git push -f origin HEAD", "git-push-force"),
        ("git push --force-with-lease origin main", "git-push-force"),
        ("git reset --hard HEAD~1", "git-reset-hard"),
        ("git clean -fd", "git-clean-force"),
        ("git clean -xffd", "git-clean-force"),
        ("git checkout -f main", "git-checkout-force"),
        ("dd if=/dev/zero of=/dev/sda", "dd"),
        ("mkfs.ext4 /dev/sdb1", "mkfs"),
        (":(){ :|:& };:", "fork-bomb"),
        ("bash -c 'rm -rf .'", "rm"),
        ('sh -c "sudo id"', "sudo"),
        ("find . -name '*.tmp' -delete", "find-delete"),
        ("find . -exec rm {} +", "rm"),
        ("xargs rm", "rm"),
        ("env FOO=1 rm file.txt", "rm"),
        ("nohup rm file.txt", "rm"),
        ("timeout 5 rm file.txt", "rm"),
        ("echo $(rm -rf .)", "command-substitution"),
        ("`rm x`", "command-substitution"),
    ],
)
def test_blocks_dangerous_commands(command: str, rule: str) -> None:
    blocked = check_command(command)
    assert blocked is not None
    assert blocked.rule == rule


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q",
        "python -m compileall src",
        "ruff check .",
        "git status",
        "git diff",
        "git commit -m 'safe'",
        "git push origin HEAD",
        "git reset",
        "git reset --soft HEAD~1",
        "git clean -n",
        "git checkout main",
        "npm install",
        "echo firmware",
        "echo rm",
        "ls remote",
        "git restore path.txt",
        "bash -c 'pytest -q'",
        "timeout 60 pytest -q",
        "xargs grep TODO",
        "find . -name '*.py'",
        "find . -exec grep -l TODO {} +",
    ],
)
def test_allows_safe_commands(command: str) -> None:
    assert check_command(command) is None


def test_run_command_tool_rejects_before_execution(tmp_path: Path) -> None:
    target = tmp_path / "keep.txt"
    target.write_text("safe", encoding="utf-8")
    result = RunCommandTool().run(
        ToolContext(workspace=tmp_path),
        command="rm -rf keep.txt",
    )
    assert not result.ok
    assert "blocked by safety guardrail" in result.output
    assert "rm" in result.output
    assert target.read_text(encoding="utf-8") == "safe"


def test_allows_python_c_with_semicolons(tmp_path: Path) -> None:
    result = RunCommandTool().run(
        ToolContext(workspace=tmp_path),
        command="python -c \"import sys; print('ok')\"",
    )
    assert result.ok
    assert "ok" in result.output


@pytest.mark.parametrize(
    "command", ["git commit -am test", "git push origin branch", "git remote -v"]
)
def test_hosted_mode_blocks_service_owned_git(command: str) -> None:
    blocked = check_command(command, hosted=True)
    assert blocked is not None
    assert blocked.rule == "hosted-git-publication"
