from __future__ import annotations

from pathlib import Path

from gca.models import ModelProfile, ModelRegistry
from gca.providers.base import LLMResponse, Message, ToolSpec
from gca.providers.scripted import ScriptedProvider
from gca.runtime import RuntimeConfig, create_coordinator
from gca.session import SessionStore


class RecordingScriptedProvider(ScriptedProvider):
    def __init__(self, steps: list[LLMResponse], final_text: str = "Done.") -> None:
        super().__init__(steps, final_text)
        self.tool_names: list[set[str]] = []

    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        self.tool_names.append({tool.name for tool in tools})
        return super().complete(messages, tools)


def _registry(
    fast: ScriptedProvider,
    strong: ScriptedProvider,
) -> ModelRegistry:
    registry = ModelRegistry()
    registry.register(ModelProfile("fast", fast, strength=2, speed=5, cost=1))
    registry.register(ModelProfile("strong", strong, strength=5, speed=2, cost=5))
    return registry


def test_small_task_uses_single_fast_agent(tmp_path: Path) -> None:
    fast = RecordingScriptedProvider.from_script(
        [{"tool_calls": [{"name": "finish", "arguments": {"summary": "Fixed typo."}}]}]
    )
    strong = RecordingScriptedProvider.from_script([])
    store = SessionStore(tmp_path / "sessions")
    session = store.create("Fix a typo in README")
    config = RuntimeConfig(
        workspace=tmp_path,
        sessions_dir=tmp_path / "sessions",
        max_steps=5,
    )

    result = create_coordinator(config, _registry(fast, strong)).run(session, store)

    assert result.status == "completed"
    assert session.workflow is not None
    assert session.workflow.name == "fast"
    assert session.active_model == "fast"
    assert len(fast.tool_names) == 1
    assert strong.tool_names == []


def test_agents_md_can_override_workflow_and_model(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text(
        ("---\ngca:\n  workflow: fast\n  models:\n    fast: strong\n---\nFollow project rules.\n"),
        encoding="utf-8",
    )
    fast = RecordingScriptedProvider.from_script([])
    strong = RecordingScriptedProvider.from_script(
        [
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {"summary": "Handled configured workflow."},
                    }
                ]
            }
        ]
    )
    store = SessionStore(tmp_path / "sessions")
    session = store.create("Add a new feature")
    config = RuntimeConfig(
        workspace=tmp_path,
        sessions_dir=tmp_path / "sessions",
        max_steps=5,
    )

    result = create_coordinator(config, _registry(fast, strong)).run(session, store)

    assert result.status == "completed"
    assert session.workflow is not None
    assert session.workflow.name == "fast"
    assert session.active_model == "strong"
    assert fast.tool_names == []
    assert len(strong.tool_names) == 1


def test_feature_workflow_routes_separate_agents(tmp_path: Path) -> None:
    strong = RecordingScriptedProvider.from_script(
        [
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {
                            "plan": "Create greeting.py, run it, and independently review it."
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
                                'python -c "import greeting; '
                                "assert greeting.greet('Ada') == 'Hello, Ada!'\""
                            )
                        },
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {
                            "verdict": "approved",
                            "summary": "Greeting behavior verified.",
                        },
                    }
                ]
            },
        ]
    )
    fast = RecordingScriptedProvider.from_script(
        [
            {
                "tool_calls": [
                    {
                        "name": "create_file",
                        "arguments": {
                            "path": "greeting.py",
                            "content": (
                                "def greet(name):\n"
                                '    """Return a greeting."""\n'
                                '    return f"Hello, {name}!"\n'
                            ),
                        },
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {"summary": "Implemented and checked greeting.py."},
                    }
                ]
            },
        ]
    )
    store = SessionStore(tmp_path / "sessions")
    session = store.create("Implement greeting behavior")
    config = RuntimeConfig(
        workspace=tmp_path,
        sessions_dir=tmp_path / "sessions",
        max_steps=10,
        workflow="feature",
    )

    result = create_coordinator(config, _registry(fast, strong)).run(session, store)

    assert result.status == "completed"
    assert result.final_message == "Greeting behavior verified."
    assert (tmp_path / "greeting.py").is_file()
    assert [run.phase for run in session.agent_runs] == [
        "planning",
        "implementation",
        "review",
    ]
    assert [run.model for run in session.agent_runs] == ["strong", "fast", "strong"]
    assert session.plan.startswith("Create greeting.py")
    assert session.workflow is not None
    assert session.workflow.artifacts["review"] == "Greeting behavior verified."

    planning_tools = strong.tool_names[0]
    review_tools = strong.tool_names[1]
    assert "create_file" not in planning_tools
    assert "run_command" not in planning_tools
    assert "run_command" in review_tools
    assert "create_file" not in review_tools


def test_review_can_request_rework(tmp_path: Path) -> None:
    strong = RecordingScriptedProvider.from_script(
        [
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {"plan": "Create value.txt containing corrected."},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {
                            "verdict": "changes_requested",
                            "summary": "value.txt should contain corrected.",
                        },
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {
                            "verdict": "approved",
                            "summary": "Correction verified.",
                        },
                    }
                ]
            },
        ]
    )
    fast = RecordingScriptedProvider.from_script(
        [
            {
                "tool_calls": [
                    {
                        "name": "create_file",
                        "arguments": {"path": "value.txt", "content": "wrong"},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {"summary": "Created value.txt."},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "write_file",
                        "arguments": {"path": "value.txt", "content": "corrected"},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {"summary": "Applied review feedback."},
                    }
                ]
            },
        ]
    )
    store = SessionStore(tmp_path / "sessions")
    session = store.create("Implement value behavior")
    config = RuntimeConfig(
        workspace=tmp_path,
        sessions_dir=tmp_path / "sessions",
        max_steps=10,
        workflow="feature",
    )

    result = create_coordinator(config, _registry(fast, strong)).run(session, store)

    assert result.status == "completed"
    assert (tmp_path / "value.txt").read_text(encoding="utf-8") == "corrected"
    assert [run.phase for run in session.agent_runs] == [
        "planning",
        "implementation",
        "review",
        "implementation",
        "review",
    ]
    assert session.workflow is not None
    assert session.workflow.review_cycles == 1


def test_feature_workflow_resumes_current_agent_and_provider_cursor(
    tmp_path: Path,
) -> None:
    strong_script = [
        {
            "tool_calls": [
                {
                    "name": "finish",
                    "arguments": {"plan": "Create resume.txt, then review it."},
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "name": "finish",
                    "arguments": {
                        "verdict": "approved",
                        "summary": "Resume path verified.",
                    },
                }
            ]
        },
    ]
    fast_script = [
        {
            "tool_calls": [
                {
                    "name": "create_file",
                    "arguments": {"path": "resume.txt", "content": "resumed"},
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "name": "finish",
                    "arguments": {"summary": "Created resume.txt."},
                }
            ]
        },
    ]
    store = SessionStore(tmp_path / "sessions")
    session = store.create("Implement resumable behavior")
    first_config = RuntimeConfig(
        workspace=tmp_path,
        sessions_dir=tmp_path / "sessions",
        max_steps=2,
        workflow="feature",
    )

    first = create_coordinator(
        first_config,
        _registry(
            RecordingScriptedProvider.from_script(fast_script),
            RecordingScriptedProvider.from_script(strong_script),
        ),
    ).run(session, store)

    assert first.status == "paused"
    reloaded = store.load(session.id)
    resumed_config = RuntimeConfig(
        workspace=tmp_path,
        sessions_dir=tmp_path / "sessions",
        max_steps=5,
        workflow="feature",
    )
    resumed = create_coordinator(
        resumed_config,
        _registry(
            RecordingScriptedProvider.from_script(fast_script),
            RecordingScriptedProvider.from_script(strong_script),
        ),
    ).run(reloaded, store)

    assert resumed.status == "completed"
    assert resumed.final_message == "Resume path verified."
    assert [run.phase for run in reloaded.agent_runs] == [
        "planning",
        "implementation",
        "review",
    ]
