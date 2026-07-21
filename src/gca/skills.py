"""Skill discovery and lazy loading.

A *skill* is a standard-operating-procedure document (``SKILL.md``) with YAML
frontmatter describing when to use it. The harness indexes available skills and
advertises their names/descriptions to the model cheaply; the full body is only
loaded on demand via the ``load_skill`` tool. Skills can live in repo-local
directories (e.g. ``.gca/skills`` or ``skills``) or any directory the caller
points at.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from gca.tools.base import Tool, ToolContext, ToolResult

_SKILL_FILENAME = "SKILL.md"


@dataclass
class Skill:
    """A single skill: metadata plus a lazily-read body."""

    name: str
    description: str
    path: Path

    def body(self) -> str:
        """Read and return the skill body (the markdown after the frontmatter)."""

        text = self.path.read_text(encoding="utf-8")
        _, body = _split_frontmatter(text)
        return body.strip()


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a ``---`` delimited YAML frontmatter block from the body."""

    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            meta = yaml.safe_load(parts[1]) or {}
            if isinstance(meta, dict):
                return meta, parts[2]
    return {}, text


class SkillRegistry:
    """An index of skills discovered under one or more directories."""

    def __init__(self, skills: list[Skill] | None = None) -> None:
        self._skills: dict[str, Skill] = {}
        for skill in skills or []:
            self._skills[skill.name] = skill

    @classmethod
    def discover(cls, roots: list[Path]) -> SkillRegistry:
        registry = cls()
        for root in roots:
            root = Path(root)
            if not root.is_dir():
                continue
            for skill_file in sorted(root.rglob(_SKILL_FILENAME)):
                skill = _load_skill_file(skill_file)
                if skill is not None:
                    registry._skills[skill.name] = skill
        return registry

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def names(self) -> list[str]:
        return sorted(self._skills)

    def catalog(self) -> str:
        """A compact, model-facing listing of available skills."""

        if not self._skills:
            return ""
        lines = ["Available skills (load a skill with the 'load_skill' tool):"]
        for name in self.names():
            skill = self._skills[name]
            lines.append(f"- {name}: {skill.description}")
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self._skills)


class LoadSkillTool(Tool):
    """Load the full body of a named skill on demand."""

    name = "load_skill"
    description = (
        "Load the full instructions for a named skill. Call this when a task matches "
        "a skill's description to retrieve its step-by-step procedure."
    )
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "The skill name to load."}},
        "required": ["name"],
    }

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry

    def run(self, ctx: ToolContext, **kwargs: object) -> ToolResult:
        name = str(kwargs.get("name", ""))
        skill = self._registry.get(name)
        if skill is None:
            available = ", ".join(self._registry.names()) or "(none)"
            return ToolResult.failure(f"unknown skill: {name}. Available: {available}")
        return ToolResult.success(skill.body())


def _load_skill_file(path: Path) -> Skill | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None
    meta, _ = _split_frontmatter(text)
    name = str(meta.get("name") or path.parent.name)
    description = str(meta.get("description") or "").strip()
    return Skill(name=name, description=description, path=path)
