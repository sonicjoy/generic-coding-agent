from __future__ import annotations

import pytest

from gca.models import ModelProfile, ModelRegistry, ModelSelectionError
from gca.providers.base import LLMProvider, LLMResponse, Message, ToolSpec


class StubProvider(LLMProvider):
    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        return LLMResponse(content="done")


def test_selects_strongest_and_efficient_models() -> None:
    registry = ModelRegistry()
    registry.register(
        ModelProfile("fast", StubProvider(), strength=2, speed=5, cost=1)
    )
    registry.register(
        ModelProfile("strong", StubProvider(), strength=5, speed=2, cost=5)
    )

    assert registry.select(capability="planning", strategy="strongest").name == "strong"
    assert registry.select(capability="coding", strategy="efficient").name == "fast"
    assert (
        registry.select(
            capability="coding",
            strategy="efficient",
            min_strength=3,
        ).name
        == "strong"
    )


def test_explicit_preference_wins_and_validates_capability() -> None:
    registry = ModelRegistry()
    registry.register(
        ModelProfile(
            "review-only",
            StubProvider(),
            capabilities=frozenset({"review"}),
        )
    )

    assert (
        registry.select(
            capability="review",
            strategy="strongest",
            preferred="review-only",
        ).name
        == "review-only"
    )
    with pytest.raises(ModelSelectionError, match="does not support"):
        registry.select(
            capability="coding",
            strategy="efficient",
            preferred="review-only",
        )


def test_profile_scores_are_one_to_five() -> None:
    with pytest.raises(ValueError, match="strength"):
        ModelProfile("invalid", StubProvider(), strength=6)
