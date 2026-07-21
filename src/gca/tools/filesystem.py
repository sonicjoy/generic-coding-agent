"""Built-in filesystem tools: explore, read, write, create, delete, move.

All paths are workspace-relative and confined to the sandbox via
:meth:`gca.tools.base.ToolContext.resolve`.
"""

from __future__ import annotations

from typing import Any

from gca.tools.base import Tool, ToolContext, ToolResult

_IGNORED_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".gca"}


class ExploreTool(Tool):
    """List the project structure as an indented tree (a quick way to orient)."""

    name = "explore"
    description = (
        "List the directory structure under a path (relative to the workspace) as an "
        "indented tree. Use this to understand project layout before reading files."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory to explore. Defaults to '.'."},
            "max_depth": {"type": "integer", "description": "Maximum depth to descend. Default 3."},
        },
    }

    def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        path = str(kwargs.get("path", "."))
        max_depth = int(kwargs.get("max_depth", 3))
        root = ctx.resolve(path)
        if not root.exists():
            return ToolResult.failure(f"path not found: {path}")
        if not root.is_dir():
            return ToolResult.failure(f"not a directory: {path}")

        lines: list[str] = []

        def walk(directory: Any, depth: int) -> None:
            if depth > max_depth:
                return
            entries = sorted(
                (e for e in directory.iterdir() if e.name not in _IGNORED_DIRS),
                key=lambda e: (e.is_file(), e.name.lower()),
            )
            for entry in entries:
                indent = "  " * depth
                suffix = "/" if entry.is_dir() else ""
                lines.append(f"{indent}{entry.name}{suffix}")
                if entry.is_dir():
                    walk(entry, depth + 1)

        rel = root.relative_to(ctx.workspace.resolve())
        lines.append(f"{rel if str(rel) != '.' else '.'}/")
        walk(root, 1)
        return ToolResult.success("\n".join(lines))


class ReadFileTool(Tool):
    """Read a text file, optionally a line range."""

    name = "read_file"
    description = "Read the contents of a text file (relative to the workspace)."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File to read."},
            "start_line": {"type": "integer", "description": "1-indexed first line (optional)."},
            "end_line": {"type": "integer", "description": "Inclusive last line (optional)."},
        },
        "required": ["path"],
    }

    def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        path = str(kwargs["path"])
        target = ctx.resolve(path)
        if not target.is_file():
            return ToolResult.failure(f"file not found: {path}")
        text = target.read_text(encoding="utf-8")
        lines = text.splitlines()
        start = kwargs.get("start_line")
        end = kwargs.get("end_line")
        if start is not None or end is not None:
            s = int(start) if start is not None else 1
            e = int(end) if end is not None else len(lines)
            lines = lines[max(s - 1, 0) : e]
            numbered = [f"{s + i:>6}| {line}" for i, line in enumerate(lines)]
        else:
            numbered = [f"{i + 1:>6}| {line}" for i, line in enumerate(lines)]
        return ToolResult.success("\n".join(numbered))


class WriteFileTool(Tool):
    """Overwrite (or create) a file with the given content."""

    name = "write_file"
    description = "Write content to a file (relative to the workspace), overwriting if it exists."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File to write."},
            "content": {"type": "string", "description": "Full new file contents."},
        },
        "required": ["path", "content"],
    }

    def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        path = str(kwargs["path"])
        content = str(kwargs["content"])
        target = ctx.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return ToolResult.success(f"wrote {len(content)} bytes to {path}")


class CreateFileTool(Tool):
    """Create a new file; fails if it already exists."""

    name = "create_file"
    description = "Create a new file (relative to the workspace). Fails if the file already exists."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File to create."},
            "content": {"type": "string", "description": "Initial file contents."},
        },
        "required": ["path"],
    }

    def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        path = str(kwargs["path"])
        content = str(kwargs.get("content", ""))
        target = ctx.resolve(path)
        if target.exists():
            return ToolResult.failure(f"already exists: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return ToolResult.success(f"created {path}")


class DeleteFileTool(Tool):
    """Delete a file."""

    name = "delete_file"
    description = "Delete a file (relative to the workspace)."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "File to delete."}},
        "required": ["path"],
    }

    def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        path = str(kwargs["path"])
        target = ctx.resolve(path)
        if not target.exists():
            return ToolResult.failure(f"not found: {path}")
        if target.is_dir():
            return ToolResult.failure(f"refusing to delete directory: {path}")
        target.unlink()
        return ToolResult.success(f"deleted {path}")


class MoveFileTool(Tool):
    """Move or rename a file."""

    name = "move_file"
    description = "Move or rename a file (both paths relative to the workspace)."
    parameters = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Existing path."},
            "destination": {"type": "string", "description": "New path."},
        },
        "required": ["source", "destination"],
    }

    def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        source = str(kwargs["source"])
        destination = str(kwargs["destination"])
        src = ctx.resolve(source)
        dst = ctx.resolve(destination)
        if not src.exists():
            return ToolResult.failure(f"source not found: {source}")
        if dst.exists():
            return ToolResult.failure(f"destination already exists: {destination}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        return ToolResult.success(f"moved {source} -> {destination}")


def filesystem_tools() -> list[Tool]:
    return [
        ExploreTool(),
        ReadFileTool(),
        WriteFileTool(),
        CreateFileTool(),
        DeleteFileTool(),
        MoveFileTool(),
    ]
