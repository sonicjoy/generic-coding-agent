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
from typing import Any

import yaml

_CONTEXT_FILENAMES = ("AGENTS.md", "CLAUDE.md")
_IGNORED_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".gca"}


class ContextConfigError(ValueError):
    """Raised when structured GCA configuration is invalid."""


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


def _frontmatter(content: str, path: Path) -> dict[str, Any]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    closing = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if closing is None:
        return {}
    try:
        metadata = yaml.safe_load("\n".join(lines[1:closing])) or {}
    except yaml.YAMLError as exc:
        raise ContextConfigError(f"invalid YAML frontmatter in {path}: {exc}") from exc
    if not isinstance(metadata, dict):
        raise ContextConfigError(f"YAML frontmatter in {path} must be a mapping")
    return metadata


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        previous = merged.get(key)
        if isinstance(previous, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(previous, value)
        else:
            merged[key] = value
    return merged


def load_gca_config(workspace: Path) -> dict[str, Any]:
    """Merge optional ``gca`` frontmatter from context files, root-first."""

    merged: dict[str, Any] = {}
    for context_file in discover_context_files(workspace):
        metadata = _frontmatter(context_file.content, context_file.path)
        raw_config = metadata.get("gca")
        if raw_config is None:
            continue
        if not isinstance(raw_config, dict):
            raise ContextConfigError(
                f"'gca' frontmatter in {context_file.path} must be a mapping"
            )
        merged = _deep_merge(merged, raw_config)
    return merged


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
