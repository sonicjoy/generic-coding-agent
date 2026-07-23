"""Budget reserve, auto-resume, and configurable reserve coverage."""

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


def test_feature_tiny_budget_gives_implementation_full_remaining(tmp_path: Path) -> None:
    """When max_steps <= review_step_reserve the reserve subtraction is skipped.

    With max_steps=5 (== default reserve) and planning costing 1 step, a
    4-step implementation must still receive the full remaining budget of 4.
    """

    strong = RecordingScriptedProvider.from_script(
        [
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {"plan": "Create and finish tiny work."},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {
                            "verdict": "approved",
                            "summary": "Tiny budget reserve skipped.",
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
                        "arguments": {"path": "tiny1.txt", "content": "a"},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "create_file",
                        "arguments": {"path": "tiny2.txt", "content": "b"},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "create_file",
                        "arguments": {"path": "tiny3.txt", "content": "c"},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {"summary": "Finished all tiny files."},
                    }
                ]
            },
        ]
    )
    store = SessionStore(tmp_path / "sessions")
    session = store.create("Tiny budget feature")
    result = create_coordinator(
        RuntimeConfig(
            workspace=tmp_path,
            sessions_dir=tmp_path / "sessions",
            max_steps=5,
            workflow="feature",
        ),
        _registry(fast, strong),
    ).run(session, store)

    assert result.status == "paused"
    assert result.outcome_kind == "budget_exhausted"
    assert "Step budget" in (result.final_message or "")
    assert [run.phase for run in session.agent_runs] == ["planning", "implementation"]
    assert session.workflow is not None
    assert session.workflow.artifacts.get("implementation") == "Finished all tiny files."
    assert (tmp_path / "tiny3.txt").read_text(encoding="utf-8") == "c"


def test_completed_implementation_review_auto_resumes_within_reserve(
    tmp_path: Path,
) -> None:
    """Finished implementation + review budget auto-resumes to completion."""

    strong = RecordingScriptedProvider.from_script(
        [
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {"plan": "Create done.txt then review."},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {
                            "verdict": "approved",
                            "summary": "Review completed within reserve.",
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
                        "arguments": {"path": "done.txt", "content": "impl"},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {"summary": "Implementation finished."},
                    }
                ]
            },
        ]
    )
    store = SessionStore(tmp_path / "sessions")
    session = store.create("Finish then auto-resume review")
    result = create_coordinator(
        RuntimeConfig(
            workspace=tmp_path,
            sessions_dir=tmp_path / "sessions",
            max_steps=8,
            workflow="feature",
        ),
        _registry(fast, strong),
    ).run(session, store)

    assert result.status == "completed"
    assert result.final_message == "Review completed within reserve."
    assert session.workflow is not None
    assert session.workflow.artifacts.get("implementation") == "Implementation finished."
    assert [run.phase for run in session.agent_runs] == [
        "planning",
        "implementation",
        "review",
    ]
    assert (tmp_path / "done.txt").read_text(encoding="utf-8") == "impl"


def test_configurable_review_step_reserve_changes_pause_boundary(tmp_path: Path) -> None:
    """A larger configured reserve pauses implementation earlier than the default."""

    (tmp_path / "AGENTS.md").write_text(
        "---\ngca:\n  review_step_reserve: 6\n---\nReserve tests.\n",
        encoding="utf-8",
    )
    strong = RecordingScriptedProvider.from_script(
        [
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {"plan": "Create reserved files."},
                    }
                ]
            }
        ]
    )
    fast = RecordingScriptedProvider.from_script(
        [
            {
                "tool_calls": [
                    {
                        "name": "create_file",
                        "arguments": {"path": "first.txt", "content": "1"},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "create_file",
                        "arguments": {"path": "second.txt", "content": "2"},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {
                            "summary": "Should not finish under larger reserve.",
                        },
                    }
                ]
            },
        ]
    )
    store = SessionStore(tmp_path / "sessions")
    session = store.create("Configurable reserve feature")
    result = create_coordinator(
        RuntimeConfig(
            workspace=tmp_path,
            sessions_dir=tmp_path / "sessions",
            max_steps=8,
            workflow="feature",
        ),
        _registry(fast, strong),
    ).run(session, store)

    assert result.status == "paused"
    assert result.outcome_kind == "budget_exhausted"
    assert session.workflow is not None
    assert session.workflow.phase == "implementation"
    assert session.workflow.artifacts.get("implementation") in (None, "")
    assert (tmp_path / "first.txt").exists()
    assert not (tmp_path / "second.txt").exists()
