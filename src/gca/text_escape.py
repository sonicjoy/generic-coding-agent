"""Tiny string helpers shared by providers and filesystem tools."""

from __future__ import annotations


def looks_json_over_escaped(content: str) -> bool:
    """Return True when ``content`` appears to contain JSON-over-escaped quotes."""

    return (
        '\\"' in content
        or content.lstrip().startswith('\\"""')
        or content.lstrip().startswith("\\'''")
    )
