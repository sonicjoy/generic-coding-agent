from __future__ import annotations

from pathlib import Path

from gca.personas import DEFAULT_PHASE_PERSONAS, load_personas
from gca.repo_config import load_repo_config
from gca.runtime import build_system_prompt
from gca.skills import SkillRegistry


def test_loads_base_and_phase_personas(tmp_path: Path) -> None:
    base = tmp_path / "base.md"
    review = tmp_path / "review.md"
    base.write_text("Base persona", encoding="utf-8")
    review.write_text("Custom reviewer", encoding="utf-8")

    personas = load_personas(base, {"review": review})

    assert personas.base == "Base persona"
    assert personas.for_phase("review") == "Custom reviewer"
    assert personas.for_phase("planning") == DEFAULT_PHASE_PERSONAS["planning"]


def test_system_prompt_uses_manifest_persona_and_strips_frontmatter(tmp_path: Path) -> None:
    (tmp_path / "persona.md").write_text("Custom base persona", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(
        "---\ngca:\n  workflow: fast\n---\nProject rules.\n",
        encoding="utf-8",
    )
    gca_dir = tmp_path / ".gca"
    gca_dir.mkdir()
    config_path = gca_dir / "config.yaml"
    config_path.write_text(
        "version: 1\ncontext:\n  persona_file: persona.md\n",
        encoding="utf-8",
    )
    config = load_repo_config(tmp_path, [config_path])

    prompt = build_system_prompt(tmp_path, SkillRegistry(), config)

    assert prompt.startswith("Custom base persona")
    assert "Project rules." in prompt
    assert "gca:" not in prompt
