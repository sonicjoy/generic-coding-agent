from __future__ import annotations

from pathlib import Path

import pytest

from gca.tools.base import ToolContext
from gca.tools.patch import PatchError, apply_patch, parse_unified_diff


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(workspace=tmp_path)


def test_modify_existing_file(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    diff = "--- a/f.txt\n+++ b/f.txt\n@@ -1,3 +1,3 @@\n alpha\n-beta\n+BETA\n gamma\n"
    summary = apply_patch(diff, _ctx(tmp_path))
    assert summary == ["updated f.txt"]
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"


def test_create_file(tmp_path: Path) -> None:
    diff = "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,2 @@\n+line1\n+line2\n"
    summary = apply_patch(diff, _ctx(tmp_path))
    assert summary == ["created new.txt"]
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "line1\nline2\n"


def test_delete_file(tmp_path: Path) -> None:
    (tmp_path / "gone.txt").write_text("bye\n", encoding="utf-8")
    diff = "--- a/gone.txt\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-bye\n"
    summary = apply_patch(diff, _ctx(tmp_path))
    assert summary == ["deleted gone.txt"]
    assert not (tmp_path / "gone.txt").exists()


def test_atomic_failure_leaves_files_untouched(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("keep\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("original\n", encoding="utf-8")
    # First file patches cleanly; second file's context does not match.
    diff = (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n-keep\n+changed\n"
        "--- a/b.txt\n+++ b/b.txt\n@@ -1,1 +1,1 @@\n-does-not-match\n+changed\n"
    )
    with pytest.raises(PatchError):
        apply_patch(diff, _ctx(tmp_path))
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "keep\n"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "original\n"


def test_parse_multiple_files(tmp_path: Path) -> None:
    diff = (
        "--- a/x.txt\n+++ b/x.txt\n@@ -1 +1 @@\n-x\n+X\n"
        "--- a/y.txt\n+++ b/y.txt\n@@ -1 +1 @@\n-y\n+Y\n"
    )
    patches = parse_unified_diff(diff)
    assert len(patches) == 2
    assert patches[0].new_path == "x.txt"
    assert patches[1].new_path == "y.txt"


def test_apply_patch_rejects_invalid_python_before_write(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text('"""ok"""\nVALUE = 1\n', encoding="utf-8")
    diff = '--- a/mod.py\n+++ b/mod.py\n@@ -1,2 +1,2 @@\n-"""ok"""\n+\\"""ok"""\n VALUE = 1\n'
    with pytest.raises(PatchError, match="syntax error"):
        apply_patch(diff, _ctx(tmp_path))
    assert (tmp_path / "mod.py").read_text(encoding="utf-8") == '"""ok"""\nVALUE = 1\n'


def test_apply_patch_allows_python_docstrings(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    diff = '--- a/mod.py\n+++ b/mod.py\n@@ -1,1 +1,2 @@\n+"""Module."""\n VALUE = 1\n'
    summary = apply_patch(diff, _ctx(tmp_path))
    assert summary == ["updated mod.py"]
    assert (tmp_path / "mod.py").read_text(encoding="utf-8") == '"""Module."""\nVALUE = 1\n'
