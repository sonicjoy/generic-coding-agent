"""Shared Markdown YAML-frontmatter parsing helpers."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml


class FrontmatterError(ValueError):
    """Raised when a Markdown frontmatter block is invalid."""


def split_frontmatter(text: str, *, source: Path | str = "<text>") -> tuple[dict[str, Any], str]:
    """Return parsed YAML metadata and the Markdown body.

    Files without a leading ``---`` block are returned unchanged with empty
    metadata. A leading block must contain a closing delimiter and a mapping.
    """

    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    closing = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if closing is None:
        raise FrontmatterError(f"unterminated YAML frontmatter in {source}")
    raw = "".join(lines[1:closing])
    try:
        metadata = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise FrontmatterError(f"invalid YAML frontmatter in {source}: {exc}") from exc
    if not isinstance(metadata, Mapping):
        raise FrontmatterError(f"YAML frontmatter in {source} must be a mapping")
    return dict(metadata), "".join(lines[closing + 1 :])
