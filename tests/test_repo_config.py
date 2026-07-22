from __future__ import annotations

from pathlib import Path

import pytest

from gca.repo_config import RepoConfigError, load_repo_config


def _write_config(workspace: Path, text: str) -> Path:
    directory = workspace / ".gca"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_manifest_personas_skills_and_fixed_command(tmp_path: Path) -> None:
    (tmp_path / "persona.md").write_text("Repository persona", encoding="utf-8")
    (tmp_path / "skills").mkdir()
    path = _write_config(
        tmp_path,
        """
version: 1
context:
  persona_file: persona.md
skills:
  dirs: [skills]
routing:
  workflow: fast
runtime:
  max_steps: 42
tools:
  fixed_commands:
    run_tests:
      description: Run tests
      argv: [python, -m, pytest]
      timeout: 90
      phases: [execute, implementation, review]
""",
    )

    config = load_repo_config(tmp_path, [path])

    assert config.context.persona_file == tmp_path / "persona.md"
    assert config.skill_dirs == (tmp_path / "skills",)
    assert config.routing.workflow == "fast"
    assert config.runtime.max_steps == 42
    assert config.tools.fixed_commands["run_tests"].argv == ("python", "-m", "pytest")


def test_frontmatter_overrides_manifest_routing(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        """
version: 1
routing:
  workflow: fast
  models:
    fast: quick
""",
    )
    (tmp_path / "AGENTS.md").write_text(
        "---\ngca:\n  workflow: feature\n  models:\n    review: critic\n---\nRules.\n",
        encoding="utf-8",
    )

    config = load_repo_config(tmp_path, [path])

    assert config.routing.workflow == "feature"
    assert config.routing.model_preferences == {"fast": "quick", "review": "critic"}


def test_rejects_path_escape(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        """
version: 1
skills:
  dirs: [../outside]
""",
    )

    with pytest.raises(RepoConfigError, match="escapes the workspace"):
        load_repo_config(tmp_path, [path])


def test_rejects_unknown_and_unversioned_config(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "unexpected: true\n")
    with pytest.raises(RepoConfigError, match="must declare version"):
        load_repo_config(tmp_path, [path])

    path.write_text("version: 1\nunexpected: true\n", encoding="utf-8")
    with pytest.raises(RepoConfigError, match="unknown keys"):
        load_repo_config(tmp_path, [path])


def test_config_fingerprint_is_stable(tmp_path: Path) -> None:
    config = load_repo_config(tmp_path, [])
    assert config.fingerprint() == load_repo_config(tmp_path, []).fingerprint()
