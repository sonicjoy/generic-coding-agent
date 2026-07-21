"""Unified-diff patch engine and the ``apply_patch`` tool.

The agent is expected to produce *patches*, not whole files. This module parses
standard unified diffs (including file creation and deletion) and applies them
safely: every hunk is validated against the current file contents first, and the
whole patch is applied atomically. If any hunk fails to locate its context, no
files are modified (implicit rollback), and a descriptive error is returned so
the agent can revise the diff.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gca.tools.base import Tool, ToolContext, ToolError, ToolResult

# How far to search around the declared hunk position before giving up.
_SEARCH_WINDOW = 200


class PatchError(Exception):
    """Raised when a unified diff cannot be parsed or applied."""


@dataclass
class Hunk:
    old_start: int
    old_lines: list[str]
    new_lines: list[str]


@dataclass
class FilePatch:
    old_path: str | None
    new_path: str | None
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def is_creation(self) -> bool:
        return self.old_path is None

    @property
    def is_deletion(self) -> bool:
        return self.new_path is None


def _strip_prefix(path: str) -> str | None:
    """Normalise a diff path, returning ``None`` for /dev/null."""

    path = path.strip()
    if path == "/dev/null":
        return None
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


def parse_unified_diff(diff: str) -> list[FilePatch]:
    """Parse a unified diff into a list of :class:`FilePatch` objects."""

    lines = diff.splitlines()
    patches: list[FilePatch] = []
    current: FilePatch | None = None
    hunk_old: list[str] = []
    hunk_new: list[str] = []
    hunk_old_start = 0
    in_hunk = False

    def flush_hunk() -> None:
        nonlocal in_hunk, hunk_old, hunk_new
        if in_hunk and current is not None:
            current.hunks.append(Hunk(hunk_old_start, hunk_old, hunk_new))
        hunk_old = []
        hunk_new = []
        in_hunk = False

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- "):
            flush_hunk()
            old = _strip_prefix(line[4:])
            if i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
                raise PatchError("malformed diff: '---' not followed by '+++'")
            new = _strip_prefix(lines[i + 1][4:])
            current = FilePatch(old_path=old, new_path=new)
            patches.append(current)
            i += 2
            continue
        if line.startswith("@@"):
            flush_hunk()
            if current is None:
                raise PatchError("hunk found before any file header")
            hunk_old_start = _parse_hunk_header(line)
            hunk_old = []
            hunk_new = []
            in_hunk = True
            i += 1
            continue
        if in_hunk:
            if line.startswith("\\"):  # "\ No newline at end of file"
                i += 1
                continue
            tag = line[:1]
            body = line[1:]
            if tag == " ":
                hunk_old.append(body)
                hunk_new.append(body)
            elif tag == "-":
                hunk_old.append(body)
            elif tag == "+":
                hunk_new.append(body)
            elif line == "":
                # A bare blank line inside a hunk is a context line.
                hunk_old.append("")
                hunk_new.append("")
            else:
                # Non-hunk content ends the current hunk.
                flush_hunk()
                continue
        i += 1

    flush_hunk()
    if not patches:
        raise PatchError("no file headers found in diff")
    return patches


def _parse_hunk_header(header: str) -> int:
    """Extract the 1-indexed old-file start line from an ``@@ -a,b +c,d @@`` header."""

    try:
        old_segment = header.split("@@")[1].strip().split(" ")[0]
        old_segment = old_segment.lstrip("-")
        old_start = int(old_segment.split(",")[0])
    except (IndexError, ValueError) as exc:
        raise PatchError(f"malformed hunk header: {header!r}") from exc
    return old_start


def _locate(haystack: list[str], needle: list[str], guess: int) -> int:
    """Find ``needle`` within ``haystack``, preferring positions near ``guess``."""

    if not needle:
        return max(0, min(guess, len(haystack)))
    if haystack[guess : guess + len(needle)] == needle:
        return guess
    lo = max(0, guess - _SEARCH_WINDOW)
    hi = min(len(haystack) - len(needle), guess + _SEARCH_WINDOW)
    for offset in range(0, hi - lo + 1):
        pos = lo + offset
        if haystack[pos : pos + len(needle)] == needle:
            return pos
    raise PatchError("hunk context not found in target file")


def _apply_hunks(original: str, hunks: list[Hunk]) -> str:
    """Apply hunks to ``original`` text, returning the new text."""

    had_trailing_newline = original.endswith("\n") or original == ""
    lines = original.splitlines()
    offset = 0
    for hunk in hunks:
        guess = max(0, hunk.old_start - 1 + offset)
        pos = _locate(lines, hunk.old_lines, guess)
        lines[pos : pos + len(hunk.old_lines)] = hunk.new_lines
        offset += len(hunk.new_lines) - len(hunk.old_lines)
    text = "\n".join(lines)
    if had_trailing_newline and text and not text.endswith("\n"):
        text += "\n"
    return text


@dataclass
class _PlannedChange:
    path: str
    action: str  # "write" | "delete"
    content: str = ""


def apply_patch(diff: str, ctx: ToolContext) -> list[str]:
    """Validate and apply a unified diff atomically. Returns a summary per file."""

    patches = parse_unified_diff(diff)
    planned: list[_PlannedChange] = []

    for patch in patches:
        if patch.is_deletion:
            assert patch.old_path is not None
            target = ctx.resolve(patch.old_path)
            if not target.is_file():
                raise PatchError(f"cannot delete missing file: {patch.old_path}")
            planned.append(_PlannedChange(path=patch.old_path, action="delete"))
            continue

        assert patch.new_path is not None
        rel_path = patch.new_path
        target = ctx.resolve(rel_path)
        if patch.is_creation:
            if target.exists():
                raise PatchError(f"cannot create existing file: {rel_path}")
            original = ""
        else:
            if not target.is_file():
                raise PatchError(f"target file not found: {rel_path}")
            original = target.read_text(encoding="utf-8")
        new_content = _apply_hunks(original, patch.hunks)
        planned.append(_PlannedChange(path=rel_path, action="write", content=new_content))

    # All hunks validated; now perform writes (atomic in aggregate).
    summary: list[str] = []
    for change in planned:
        target = ctx.resolve(change.path)
        if change.action == "delete":
            target.unlink()
            summary.append(f"deleted {change.path}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            existed = target.exists()
            target.write_text(change.content, encoding="utf-8")
            summary.append(f"{'updated' if existed else 'created'} {change.path}")
    return summary


class ApplyPatchTool(Tool):
    """Apply a unified diff to the workspace (create/modify/delete files)."""

    name = "apply_patch"
    description = (
        "Apply a unified diff (git-style) to the workspace. Supports modifying, "
        "creating (--- /dev/null) and deleting (+++ /dev/null) files. The patch is "
        "validated first and applied atomically; on any failure nothing is changed."
    )
    parameters = {
        "type": "object",
        "properties": {
            "diff": {"type": "string", "description": "The unified diff text to apply."}
        },
        "required": ["diff"],
    }

    def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        diff = str(kwargs["diff"])
        try:
            summary = apply_patch(diff, ctx)
        except (PatchError, ToolError) as exc:
            return ToolResult.failure(f"patch failed: {exc}")
        return ToolResult.success("\n".join(summary))


def patch_tools() -> list[Tool]:
    return [ApplyPatchTool()]


__all__ = [
    "ApplyPatchTool",
    "FilePatch",
    "Hunk",
    "PatchError",
    "apply_patch",
    "parse_unified_diff",
    "patch_tools",
]
