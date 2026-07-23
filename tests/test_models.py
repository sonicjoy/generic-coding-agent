from __future__ import annotations

import pytest

from gca.models import ModelProfile, ModelRegistry, ModelSelectionError
from gca.providers.base import LLMProvider, LLMResponse, Message, ToolSpec


class StubProvider(LLMProvider):
    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        return LLMResponse(content="done")


def test_selects_strongest_and_efficient_models() -> None:
    registry = ModelRegistry()
    registry.register(ModelProfile("fast", StubProvider(), strength=2, speed=5, cost=1))
    registry.register(ModelProfile("strong", StubProvider(), strength=5, speed=2, cost=5))

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
    with pytest.raises(ModelSelectionError, match="below the required minimum"):
        registry.select(
            capability="review",
            strategy="strongest",
            preferred="review-only",
            min_strength=4,
        )
    with pytest.raises(ModelSelectionError, match="tool_use"):
        registry.select(
            capability="review",
            strategy="strongest",
            preferred="review-only",
            additional_capabilities=frozenset({"tool_use"}),
        )


def test_preferred_list_falls_back_when_primary_unavailable() -> None:
    registry = ModelRegistry()
    registry.register(ModelProfile("opus", StubProvider(), strength=5))

    assert (
        registry.select(
            capability="planning",
            strategy="strongest",
            preferred=["fable", "opus"],
        ).name
        == "opus"
    )


def test_preferred_list_keeps_primary_when_available() -> None:
    registry = ModelRegistry()
    registry.register(ModelProfile("fable", StubProvider(), strength=5))
    registry.register(ModelProfile("opus", StubProvider(), strength=5))

    assert (
        registry.select(
            capability="planning",
            strategy="strongest",
            preferred=["fable", "opus"],
        ).name
        == "fable"
    )


def test_profile_scores_are_one_to_five() -> None:
    with pytest.raises(ValueError, match="strength"):
        ModelProfile("invalid", StubProvider(), strength=6)
