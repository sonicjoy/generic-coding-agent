from __future__ import annotations

from pathlib import Path

from gca.context import build_context_prompt, discover_context_files


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
