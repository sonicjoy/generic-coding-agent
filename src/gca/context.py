"""Discovery and merging of ``AGENTS.md`` instruction files.

Following the convention used by tools like Cursor and Claude Code, the harness
loads ``AGENTS.md`` files from the workspace and injects them into the system
context. Nested files are supported: a file deeper in the tree is more specific,
so files are concatenated from the root downward with clear provenance headers.
``CLAUDE.md`` is also recognised for compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_CONTEXT_FILENAMES = ("AGENTS.md", "CLAUDE.md")
_IGNORED_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".gca"}


@dataclass
class ContextFile:
    path: Path
    content: str


def discover_context_files(workspace: Path) -> list[ContextFile]:
    """Find all context files under ``workspace``, ordered root-first (shallowest)."""

    workspace = Path(workspace).resolve()
    found: list[ContextFile] = []
    for path in sorted(workspace.rglob("*")):
        if not path.is_file() or path.name not in _CONTEXT_FILENAMES:
            continue
        if any(part in _IGNORED_DIRS for part in path.relative_to(workspace).parts):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        found.append(ContextFile(path=path, content=content))
    found.sort(key=lambda cf: len(cf.path.relative_to(workspace).parts))
    return found


def build_context_prompt(workspace: Path) -> str:
    """Return a single string merging all discovered context files, or ''."""

    workspace = Path(workspace).resolve()
    files = discover_context_files(workspace)
    if not files:
        return ""
    blocks: list[str] = []
    for cf in files:
        rel = cf.path.relative_to(workspace)
        blocks.append(f"--- Project instructions from {rel} ---\n{cf.content.strip()}")
    return "\n\n".join(blocks)
