from __future__ import annotations

from pathlib import Path

import pytest

from gca.tools.base import ExecutionPolicy, ToolContext, ToolError
from gca.tools.filesystem import (
    CreateFileTool,
    DeleteFileTool,
    ExploreTool,
    MoveFileTool,
    ReadFileTool,
    WriteFileTool,
)
from gca.tools.search import SearchTool


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(workspace=tmp_path)


def test_write_then_read(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    WriteFileTool().run(ctx, path="dir/hello.txt", content="hi\nthere\n")
    result = ReadFileTool().run(ctx, path="dir/hello.txt")
    assert result.ok
    assert "hi" in result.output and "there" in result.output


def test_create_rejects_existing(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    assert CreateFileTool().run(ctx, path="a.txt", content="x").ok
    assert not CreateFileTool().run(ctx, path="a.txt", content="y").ok


def test_move_and_delete(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    WriteFileTool().run(ctx, path="a.txt", content="data")
    assert MoveFileTool().run(ctx, source="a.txt", destination="b.txt").ok
    assert (tmp_path / "b.txt").exists()
    assert not (tmp_path / "a.txt").exists()
    assert DeleteFileTool().run(ctx, path="b.txt").ok
    assert not (tmp_path / "b.txt").exists()


def test_explore_lists_tree(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    WriteFileTool().run(ctx, path="pkg/mod.py", content="x")
    out = ExploreTool().run(ctx, path=".").output
    assert "pkg/" in out
    assert "mod.py" in out


def test_search_finds_matches(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    WriteFileTool().run(ctx, path="code.py", content="def foo():\n    return 1\n")
    result = SearchTool().run(ctx, pattern=r"def \w+", glob=".py")
    assert result.ok
    assert "code.py:1" in result.output


def test_path_escape_is_blocked(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        ctx.resolve("../secret.txt")
    except ToolError:
        return
    raise AssertionError("expected ToolError for path escape")


def test_read_file_enforces_size_limit(tmp_path: Path) -> None:
    (tmp_path / "large.txt").write_text("0123456789", encoding="utf-8")
    ctx = ToolContext(
        workspace=tmp_path,
        execution=ExecutionPolicy(max_read_bytes=5),
    )

    result = ReadFileTool().run(ctx, path="large.txt")

    assert not result.ok
    assert "exceeds read limit" in result.output


@pytest.mark.parametrize(
    "relative",
    [".env", ".env.local", ".gca/.env", ".gca/sessions/run.json", ".git/config"],
)
def test_secret_and_runtime_paths_are_protected(tmp_path: Path, relative: str) -> None:
    with pytest.raises(ToolError, match="protected"):
        _ctx(tmp_path).resolve(relative)


def test_env_example_remains_accessible(tmp_path: Path) -> None:
    target = _ctx(tmp_path).resolve(".env.example")
    assert target == tmp_path / ".env.example"


def test_explore_and_search_hide_protected_files(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TOKEN=secret-marker\n", encoding="utf-8")

    explored = ExploreTool().run(_ctx(tmp_path), path=".").output
    searched = SearchTool().run(_ctx(tmp_path), pattern="secret-marker").output

    assert ".env" not in explored
    assert searched == "no matches"


def test_hosted_repository_policy_is_immutable(tmp_path: Path) -> None:
    context = ToolContext(
        workspace=tmp_path,
        execution=ExecutionPolicy(profile="hosted"),
    )

    with pytest.raises(ToolError, match="immutable"):
        context.resolve(".gca/config.yaml")
