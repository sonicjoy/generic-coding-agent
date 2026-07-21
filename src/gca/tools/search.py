"""Built-in code-search tool (regex across text files)."""

from __future__ import annotations

import re
from typing import Any

from gca.tools.base import Tool, ToolContext, ToolResult

_IGNORED_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".gca"}
_MAX_MATCHES = 200


class SearchTool(Tool):
    """Search file contents with a regular expression, returning ``path:line: text``."""

    name = "search"
    description = (
        "Search text files under a path for a regular expression. Returns matching "
        "lines as 'path:line: text'. Use to locate symbols, imports, or usages."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python regular expression."},
            "path": {"type": "string", "description": "Directory to search. Defaults to '.'."},
            "glob": {
                "type": "string",
                "description": "Optional filename suffix filter, e.g. '.py'.",
            },
        },
        "required": ["pattern"],
    }

    def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        pattern = str(kwargs["pattern"])
        path = str(kwargs.get("path", "."))
        suffix = kwargs.get("glob")
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return ToolResult.failure(f"invalid regex: {exc}")

        root = ctx.resolve(path)
        if not root.exists():
            return ToolResult.failure(f"path not found: {path}")

        workspace_root = ctx.workspace.resolve()
        matches: list[str] = []
        files = [root] if root.is_file() else sorted(root.rglob("*"))
        for file in files:
            if not file.is_file():
                continue
            if any(part in _IGNORED_DIRS for part in file.parts):
                continue
            if suffix and not file.name.endswith(str(suffix)):
                continue
            try:
                text = file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            rel = file.relative_to(workspace_root)
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(f"{rel}:{lineno}: {line.strip()}")
                    if len(matches) >= _MAX_MATCHES:
                        matches.append(f"... (truncated at {_MAX_MATCHES} matches)")
                        return ToolResult.success("\n".join(matches))
        if not matches:
            return ToolResult.success("no matches")
        return ToolResult.success("\n".join(matches))


def search_tools() -> list[Tool]:
    return [SearchTool()]
