from __future__ import annotations

from pathlib import Path

import pytest

from gca.context import (
    ContextConfigError,
    build_context_prompt,
    discover_context_files,
    load_gca_config,
)


def test_discovers_nested_root_first(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("root rules", encoding="utf-8")
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("pkg rules", encoding="utf-8")

    files = discover_context_files(tmp_path)
    assert [f.path.name for f in files] == ["AGENTS.md", "AGENTS.md"]
    # Shallowest (root) comes first.
    assert files[0].path.parent == tmp_path
    assert files[1].path.parent == sub


def test_build_prompt_merges(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("root rules", encoding="utf-8")
    prompt = build_context_prompt(tmp_path)
    assert "root rules" in prompt
    assert "AGENTS.md" in prompt


def test_empty_when_none(tmp_path: Path) -> None:
    assert build_context_prompt(tmp_path) == ""


def test_loads_and_merges_gca_frontmatter_root_first(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text(
        "---\ngca:\n  workflow: fast\n  models:\n    planning: strong\n---\nroot rules\n",
        encoding="utf-8",
    )
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "AGENTS.md").write_text(
        "---\ngca:\n  workflow: feature\n  models:\n    review: critic\n---\npackage rules\n",
        encoding="utf-8",
    )

    config = load_gca_config(tmp_path)

    assert config["workflow"] == "feature"
    assert config["models"] == {"planning": "strong", "review": "critic"}
    prompt = build_context_prompt(tmp_path)
    assert "root rules" in prompt
    assert "package rules" in prompt
    assert "gca:" not in prompt


def test_can_preserve_frontmatter_for_legacy_prompt(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text(
        "---\ngca:\n  workflow: fast\n---\nRules.\n",
        encoding="utf-8",
    )

    prompt = build_context_prompt(tmp_path, include_frontmatter=True)

    assert "gca:" in prompt


def test_context_symlink_cannot_escape_workspace(tmp_path: Path) -> None:
    external = tmp_path.parent / f"{tmp_path.name}-outside.md"
    external.write_text("outside secret", encoding="utf-8")
    (tmp_path / "AGENTS.md").symlink_to(external)

    assert build_context_prompt(tmp_path) == ""


def test_rejects_invalid_gca_frontmatter(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text(
        "---\ngca: invalid\n---\nrules\n",
        encoding="utf-8",
    )

    with pytest.raises(ContextConfigError, match="must be a mapping"):
        load_gca_config(tmp_path)
