from __future__ import annotations

from pathlib import Path

import pytest

from gca.repo_config import load_repo_config
from gca.tool_policy import (
    ToolPolicyError,
    register_fixed_commands,
    tool_names_for_phase,
    validate_tool_policy,
)
from gca.tools import build_registry


def _config(tmp_path: Path, body: str):
    config_dir = tmp_path / ".gca"
    config_dir.mkdir(exist_ok=True)
    path = config_dir / "config.yaml"
    path.write_text(f"version: 1\n{body}", encoding="utf-8")
    return load_repo_config(tmp_path, [path])


def test_hosted_profile_hides_shell_but_exposes_fixed_checks(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        """
runtime:
  profile: hosted
tools:
  fixed_commands:
    run_tests:
      argv: [python, -m, pytest]
      phases: [execute, implementation, review]
""",
    )
    registry = build_registry()
    register_fixed_commands(registry, config)

    execute = tool_names_for_phase(registry, config, "execute", workflow="fast")
    review = tool_names_for_phase(registry, config, "review", workflow="feature")

    assert "run_command" not in execute
    assert "run_command" not in review
    assert "run_tests" in execute
    assert "run_tests" in review


def test_manifest_phase_override_and_global_deny(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        """
tools:
  deny: [delete_file]
  phases:
    planning:
      allow: [read_file, search]
""",
    )
    registry = build_registry()

    planning = tool_names_for_phase(registry, config, "planning", workflow="feature")
    execute = tool_names_for_phase(registry, config, "execute", workflow="fast")

    assert planning == frozenset({"finish", "read_file", "search"})
    assert "delete_file" not in execute


def test_unknown_tool_policy_fails_closed(tmp_path: Path) -> None:
    config = _config(tmp_path, "tools:\n  deny: [does_not_exist]\n")

    with pytest.raises(ToolPolicyError, match="unavailable"):
        validate_tool_policy(build_registry(), config)
