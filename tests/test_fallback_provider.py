from __future__ import annotations

import pytest

from gca.providers.base import LLMProvider, LLMResponse, Message, ProviderError, ToolSpec
from gca.providers.fallback import FallbackProvider


class _SequenceProvider(LLMProvider):
    def __init__(self, *, error: Exception | None = None, content: str = "ok") -> None:
        self.error = error
        self.content = content
        self.calls = 0

    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return LLMResponse(content=self.content)


def test_fallback_provider_advances_on_retryable_error() -> None:
    primary = _SequenceProvider(error=ProviderError("timeout", retryable=True))
    secondary = _SequenceProvider(content="from-opus")
    events: list[tuple[str, str, str]] = []
    provider = FallbackProvider(
        [("fable", primary), ("opus", secondary)],
        on_failover=lambda old, new, err: events.append((old, new, err)),
    )

    response = provider.complete([], [])

    assert response.content == "from-opus"
    assert provider.active_name == "opus"
    assert primary.calls == 1
    assert secondary.calls == 1
    assert events == [("fable", "opus", "timeout")]


def test_fallback_provider_does_not_advance_on_non_retryable_error() -> None:
    primary = _SequenceProvider(error=ProviderError("bad request", retryable=False))
    secondary = _SequenceProvider(content="unused")
    provider = FallbackProvider([("fable", primary), ("opus", secondary)])

    with pytest.raises(ProviderError, match="bad request"):
        provider.complete([], [])
    assert provider.active_name == "fable"
    assert secondary.calls == 0


def test_fallback_provider_restores_by_active_name() -> None:
    primary = _SequenceProvider(content="fable")
    secondary = _SequenceProvider(content="opus")
    provider = FallbackProvider([("fable", primary), ("opus", secondary)])
    provider.complete([], [])
    provider._index = 1  # noqa: SLF001 - simulate prior failover
    state = provider.get_state()
    assert state["active_name"] == "opus"

    # Rebuild chain starting at the bound primary (opus only remaining prefs).
    resumed = FallbackProvider([("opus", secondary), ("unused", primary)])
    resumed.set_state(state)
    assert resumed.active_name == "opus"


def test_fallback_provider_forwards_bare_provider_state() -> None:
    class Stateful(_SequenceProvider):
        def __init__(self) -> None:
            super().__init__(content="ok")
            self.restored: dict | None = None

        def set_state(self, state: dict) -> None:
            self.restored = state

    primary = Stateful()
    provider = FallbackProvider([("fable", primary)])
    provider.set_state({"index": 3})
    assert primary.restored == {"index": 3}
