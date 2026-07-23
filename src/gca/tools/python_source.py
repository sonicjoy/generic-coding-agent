"""Helpers for validating Python source written by filesystem tools."""

from __future__ import annotations

import ast


def validate_python_source(path: str, content: str) -> str | None:
    """Return an error message when ``content`` is invalid Python for ``path``.

    Non-``.py`` paths are skipped. Callers should treat a non-``None`` return as
    a hard tool failure so the model can revise over-escaped or broken source
    instead of leaving unparseable files on disk.
    """

    if not path.endswith(".py"):
        return None
    try:
        ast.parse(content, filename=path)
    except SyntaxError as exc:
        detail = exc.msg
        if exc.lineno is not None:
            detail = f"{detail} (line {exc.lineno})"
        hint = ""
        if looks_json_over_escaped(content):
            hint = (
                " Content looks JSON-over-escaped (literal backslash-quotes). "
                "Send normal Python source in tool arguments, not an escaped JSON string."
            )
        return f"syntax error in {path}: {detail}.{hint}".rstrip()
    return None


def looks_json_over_escaped(content: str) -> bool:
    """Return True when ``content`` appears to contain JSON-over-escaped quotes."""

    return (
        '\\"' in content
        or content.lstrip().startswith('\\"""')
        or content.lstrip().startswith("\\'''")
    )
