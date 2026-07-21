from __future__ import annotations

import sys
from pathlib import Path

from gca.providers.scripted import ScriptedProvider
from gca.runtime import RuntimeConfig, create_agent
from gca.session import SessionStore


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
