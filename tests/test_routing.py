from __future__ import annotations

import pytest

from gca.complexity import classify_task
from gca.routing import RoutingConfigError, RoutingPolicy


def test_small_task_uses_fast_workflow() -> None:
    assessment = classify_task("Fix a typo in README.md", RoutingPolicy())

    assert assessment.level == "small"
    assert assessment.recommended_workflow == "fast"


def test_new_feature_and_large_change_use_feature_workflow() -> None:
    policy = RoutingPolicy()

    feature = classify_task("Add a new feature for saved searches", policy)
    large = classify_task(
        "Refactor the architecture and migrate authentication across modules",
        policy,
    )

    assert feature.level in {"medium", "large"}
    assert feature.recommended_workflow == "feature"
    assert large.level == "large"
    assert large.recommended_workflow == "feature"

    format_feature = classify_task("Add support for a new JSON format", policy)
    assert format_feature.recommended_workflow == "feature"
    assert classify_task("Support a JSON format", policy).recommended_workflow == "feature"
    assert classify_task("Migrate documentation", policy).level == "large"


def test_policy_parses_overrides() -> None:
    policy = RoutingPolicy.from_mapping(
        {
            "workflow": "feature",
            "models": {"planning": "top", "fast": "mini"},
            "minimum_strength": {"implementation": 2},
            "max_review_cycles": 3,
            "complexity": {"feature_threshold": 2, "large_threshold": 5},
        }
    )

    assert policy.workflow == "feature"
    assert policy.preferred_model("planning") == "top"
    assert policy.min_strength("implementation", "medium") == 2
    assert policy.min_strength("implementation", "large") == 3
    assert policy.max_review_cycles == 3
    assert policy.choose_workflow("auto", "fast") == "fast"


def test_policy_rejects_unknown_model_role() -> None:
    with pytest.raises(RoutingConfigError, match="unknown models roles"):
        RoutingPolicy.from_mapping({"models": {"unknown": "model"}})
