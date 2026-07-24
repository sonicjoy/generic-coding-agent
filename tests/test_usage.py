from __future__ import annotations

import json
from pathlib import Path

from gca.agent import Agent, AgentConfig
from gca.providers import openai_compatible
from gca.providers.openai_compatible import OpenAICompatibleProvider
from gca.providers.scripted import ScriptedProvider
from gca.session import SessionStore
from gca.tools import build_registry
from gca.tools.base import ToolContext
from gca.usage import LLMUsage, merge_usage, totals_from_dict


class _FakeResponse:
    def __init__(self, payload: dict[str, object], headers: dict[str, str]) -> None:
        self._payload = json.dumps(payload).encode()
        self.headers = headers

    def read(self, size: int = -1) -> bytes:
        return self._payload[:size] if size >= 0 else self._payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_openai_provider_captures_usage_and_openrouter_cost(monkeypatch: object) -> None:
    monkeypatch.setenv("TEST_LLM_KEY", "secret")  # type: ignore[attr-defined]

    def fake_open(request: object, timeout: object) -> _FakeResponse:
        _ = request, timeout
        return _FakeResponse(
            {
                "choices": [{"message": {"content": "ok", "tool_calls": []}}],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "total_tokens": 18,
                },
            },
            {
                "x-openrouter-cost": "0.0123",
                "x-openrouter-generation-id": "gen-abc",
            },
        )

    monkeypatch.setattr(openai_compatible, "_open_url", fake_open)  # type: ignore[attr-defined]
    provider = OpenAICompatibleProvider(
        model_id="openrouter/model",
        base_url="https://example.test/v1",
        api_key_env="TEST_LLM_KEY",
    )

    response = provider.complete([], [])

    assert response.usage is not None
    assert response.usage.prompt_tokens == 11
    assert response.usage.completion_tokens == 7
    assert response.usage.cost_usd == 0.0123
    assert response.usage.generation_id == "gen-abc"


def test_agent_accumulates_session_usage(tmp_path: Path) -> None:
    provider = ScriptedProvider.from_script(
        [
            {
                "tool_calls": [{"name": "finish", "arguments": {"summary": "done"}}],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 2,
                    "total_tokens": 7,
                    "cost_usd": 0.01,
                    "model": "scripted",
                },
            }
        ]
    )
    store = SessionStore(tmp_path / "sessions")
    session = store.create("task")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    registry = build_registry()
    Agent(
        provider=provider,
        registry=registry,
        session=session,
        context=ToolContext(workspace=workspace),
        store=store,
        config=AgentConfig(max_steps=3),
    ).run()

    assert session.llm_usage["prompt_tokens"] == 5
    assert session.llm_usage["completion_tokens"] == 2
    assert session.llm_usage["cost_usd"] == 0.01
    assert session.llm_usage["by_model"]["scripted"]["prompt_tokens"] == 5


def test_merge_usage_totals() -> None:
    totals = merge_usage(
        totals_from_dict({}),
        LLMUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3, cost_usd=0.5, model="a"),
    )
    totals = merge_usage(
        totals,
        LLMUsage(prompt_tokens=4, completion_tokens=1, total_tokens=5, cost_usd=0.25, model="a"),
    )
    assert totals.prompt_tokens == 5
    assert totals.completion_tokens == 3
    assert totals.cost_usd == 0.75
