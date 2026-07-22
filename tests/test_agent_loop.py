from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from gca.agent import Agent
from gca.providers.base import Message, ToolCall
from gca.providers.scripted import ScriptedProvider
from gca.runtime import RuntimeConfig, create_agent
from gca.session import Session, SessionStore
from gca.tools import build_registry
from gca.tools.base import Tool, ToolContext, ToolResult


class ExplodingTool(Tool):
    name = "explode"
    description = "Raise an unexpected exception."
    parameters = {"type": "object", "properties": {}}

    def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        raise RuntimeError("boom")


def test_end_to_end_multi_tool_task(tmp_path: Path) -> None:
    """Scripted provider drives create -> patch -> run -> finish to completion."""

    workspace = tmp_path / "ws"
    workspace.mkdir()

    script = [
        {
            "tool_calls": [
                {
                    "name": "create_file",
                    "arguments": {
                        "path": "greeting.py",
                        "content": 'def greet(name):\n    return "Hi, " + name\n',
                    },
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "name": "apply_patch",
                    "arguments": {
                        "diff": (
                            "--- a/greeting.py\n"
                            "+++ b/greeting.py\n"
                            "@@ -1,2 +1,2 @@\n"
                            " def greet(name):\n"
                            '-    return "Hi, " + name\n'
                            '+    return "Hello, " + name\n'
                        )
                    },
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "name": "run_command",
                    "arguments": {
                        "command": (
                            f"{sys.executable} -c \"import sys; sys.path.insert(0, '.'); "
                            "import greeting; print(greeting.greet('World'))\""
                        )
                    },
                }
            ]
        },
        {"tool_calls": [{"name": "finish", "arguments": {"summary": "Added greeting module."}}]},
    ]

    provider = ScriptedProvider.from_script(script)
    store = SessionStore(tmp_path / "sessions")
    session = store.create("Create a greeting module")
    config = RuntimeConfig(workspace=workspace, sessions_dir=tmp_path / "sessions")
    agent = create_agent(config, provider, session, store)

    result = agent.run()

    assert result.status == "completed"
    assert result.final_message == "Added greeting module."
    content = (workspace / "greeting.py").read_text(encoding="utf-8")
    assert content == 'def greet(name):\n    return "Hello, " + name\n'

    run_output = [m.content for m in session.messages if m.role == "tool"]
    assert any("Hello, World" in out for out in run_output)

    # Session persisted and resumable.
    reloaded = store.load(session.id)
    assert reloaded.status == "completed"


def test_scripted_provider_resumes_from_saved_cursor(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    script = [
        {"tool_calls": [{"name": "explore", "arguments": {"path": "."}}]},
        {"tool_calls": [{"name": "finish", "arguments": {"summary": "Resumed correctly."}}]},
    ]
    store = SessionStore(tmp_path / "sessions")
    session = store.create("Inspect then finish")

    first = create_agent(
        RuntimeConfig(
            workspace=workspace,
            sessions_dir=tmp_path / "sessions",
            max_steps=1,
        ),
        ScriptedProvider.from_script(script),
        session,
        store,
    ).run()

    assert first.status == "paused"
    reloaded = store.load(session.id)
    resumed = create_agent(
        RuntimeConfig(
            workspace=workspace,
            sessions_dir=tmp_path / "sessions",
            max_steps=2,
        ),
        ScriptedProvider.from_script(script),
        reloaded,
        store,
    ).run()

    assert resumed.status == "completed"
    assert resumed.final_message == "Resumed correctly."


def test_scripted_provider_rejects_different_resume_script(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    original = [{"tool_calls": [{"name": "explore", "arguments": {"path": "."}}]}]
    store = SessionStore(tmp_path / "sessions")
    session = store.create("Inspect")
    create_agent(
        RuntimeConfig(
            workspace=workspace,
            sessions_dir=tmp_path / "sessions",
            max_steps=1,
        ),
        ScriptedProvider.from_script(original),
        session,
        store,
    ).run()
    reloaded = store.load(session.id)
    replacement = [{"tool_calls": [{"name": "read_file", "arguments": {"path": "README.md"}}]}]

    with pytest.raises(ValueError, match="does not match"):
        create_agent(
            RuntimeConfig(
                workspace=workspace,
                sessions_dir=tmp_path / "sessions",
                max_steps=2,
            ),
            ScriptedProvider.from_script(replacement),
            reloaded,
            store,
        )


def test_resumes_persisted_unanswered_tool_call(tmp_path: Path) -> None:
    session = Session(
        id="pending",
        task="Finish",
        messages=[
            Message(
                role="assistant",
                tool_calls=[
                    ToolCall(
                        id="pending_finish",
                        name="finish",
                        arguments={"summary": "Recovered pending call."},
                    )
                ],
            )
        ],
        step_count=1,
    )
    store = SessionStore(tmp_path / "sessions")
    store.save(session)

    result = Agent(
        provider=ScriptedProvider.from_script([]),
        registry=build_registry(),
        session=session,
        context=ToolContext(workspace=tmp_path),
        store=store,
    ).run()

    assert result.status == "completed"
    assert result.steps == 1
    assert result.final_message == "Recovered pending call."


def test_fatal_tool_error_cannot_be_overwritten_by_finish(tmp_path: Path) -> None:
    provider = ScriptedProvider.from_script(
        [
            {
                "tool_calls": [
                    {"name": "explode", "arguments": {}},
                    {"name": "finish", "arguments": {"summary": "Should not finish."}},
                ]
            }
        ]
    )
    session = Session(id="fatal", task="Fail safely")

    result = Agent(
        provider=provider,
        registry=build_registry([ExplodingTool()]),
        session=session,
        context=ToolContext(workspace=tmp_path),
    ).run()

    assert result.status == "failed"
    assert "raised RuntimeError: boom" in result.final_message
