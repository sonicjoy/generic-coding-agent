"""Configurable base and workflow-phase personas."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PHASE_PERSONAS = {
    "planning": (
        "You are the planning agent in a multi-agent feature workflow. Inspect the "
        "workspace with read-only tools. Do not edit files or run commands. Produce "
        "a concrete implementation and verification plan, then call finish(plan=...)."
    ),
    "implementation": (
        "You are the implementation agent in a multi-agent feature workflow. Follow "
        "the approved plan and any review feedback. Edit only what the task requires, "
        "run relevant checks, and call finish(summary=...) when implementation is ready "
        "for independent review."
    ),
    "review": (
        "You are the independent review agent in a multi-agent feature workflow. "
        "Do not modify files. Inspect the implementation and run relevant checks. "
        "Call finish(verdict='approved'|'changes_requested', summary=...) with concrete "
        "evidence or actionable findings."
    ),
}


@dataclass(frozen=True)
class PersonaSet:
    """Resolved persona text for a run."""

    base: str | None = None
    roles: dict[str, str] = field(default_factory=dict)

    def for_phase(self, phase: str) -> str:
        """Return configured phase text or the built-in phase persona."""

        return self.roles.get(phase, DEFAULT_PHASE_PERSONAS.get(phase, ""))


def load_personas(
    base_file: Path | None,
    role_files: dict[str, Path],
) -> PersonaSet:
    """Read optional persona files after their paths have been validated."""

    base = _read_optional(base_file)
    roles = {role: path.read_text(encoding="utf-8").strip() for role, path in role_files.items()}
    return PersonaSet(base=base, roles=roles)


def _read_optional(path: Path | None) -> str | None:
    if path is None:
        return None
    return path.read_text(encoding="utf-8").strip()
