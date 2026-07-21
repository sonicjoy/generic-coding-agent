from __future__ import annotations

from pathlib import Path

from gca.skills import LoadSkillTool, SkillRegistry
from gca.tools.base import ToolContext


def _write_skill(root: Path, name: str, description: str, body: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )


def test_discover_and_load_body(tmp_path: Path) -> None:
    _write_skill(tmp_path, "greet", "make a greeting", "Step 1. Do it.")
    registry = SkillRegistry.discover([tmp_path])
    assert registry.names() == ["greet"]
    skill = registry.get("greet")
    assert skill is not None
    assert skill.description == "make a greeting"
    assert "Step 1. Do it." in skill.body()


def test_catalog_and_load_tool(tmp_path: Path) -> None:
    _write_skill(tmp_path, "greet", "make a greeting", "The procedure.")
    registry = SkillRegistry.discover([tmp_path])
    assert "greet: make a greeting" in registry.catalog()

    tool = LoadSkillTool(registry)
    ctx = ToolContext(workspace=tmp_path)
    ok = tool.run(ctx, name="greet")
    assert ok.ok and "The procedure." in ok.output
    missing = tool.run(ctx, name="nope")
    assert not missing.ok
