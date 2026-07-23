from __future__ import annotations

import pytest

from gca.complexity import classify_task, task_text_for_classification
from gca.integrations.webhooks import issue_task
from gca.routing import RoutingConfigError, RoutingPolicy


def test_small_task_uses_fast_workflow() -> None:
    assessment = classify_task("Fix a typo in README.md", RoutingPolicy())

    assert assessment.level == "small"
    assert assessment.recommended_workflow == "fast"


def test_scm_issue_task_classifies_on_title_not_description_keywords() -> None:
    """Description meta-language must not inflate labeled-issue complexity."""

    task = issue_task(
        "Change the README H1 title",
        (
            "Please update the top-level heading only.\n\n"
            "This was validated in an end-to-end hosted webhook run; do not "
            "refactor authentication or migrate the architecture."
        ),
    )

    assert task_text_for_classification(task) == "Change the README H1 title"
    assessment = classify_task(task, RoutingPolicy())

    assert assessment.level == "small"
    assert assessment.recommended_workflow == "fast"


def test_scm_issue_title_large_keywords_still_select_feature() -> None:
    task = issue_task(
        "Refactor authentication across modules",
        "Small clarifying note in the body.",
    )

    assessment = classify_task(task, RoutingPolicy())

    assert assessment.level == "large"
    assert assessment.recommended_workflow == "feature"


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
    assert policy.preferred_models("planning") == ("top",)
    assert policy.min_strength("implementation", "medium") == 2
    assert policy.min_strength("implementation", "large") == 3
    assert policy.max_review_cycles == 3
    assert policy.choose_workflow("auto", "fast") == "fast"


def test_policy_parses_model_fallback_lists() -> None:
    policy = RoutingPolicy.from_mapping(
        {
            "models": {
                "planning": ["claude-fable-5", "claude-opus-4.8"],
                "review": ["claude-fable-5", "claude-opus-4.8"],
            }
        }
    )

    assert policy.preferred_models("planning") == ("claude-fable-5", "claude-opus-4.8")
    assert policy.preferred_model("planning") == "claude-fable-5"
    assert policy.preferred_models("review") == ("claude-fable-5", "claude-opus-4.8")


def test_policy_rejects_duplicate_model_fallback() -> None:
    with pytest.raises(RoutingConfigError, match="duplicate model"):
        RoutingPolicy.from_mapping({"models": {"planning": ["a", "a"]}})


def test_policy_rejects_unknown_model_role() -> None:
    with pytest.raises(RoutingConfigError, match="unknown models roles"):
        RoutingPolicy.from_mapping({"models": {"unknown": "model"}})


def test_policy_parses_review_step_reserve() -> None:
    policy = RoutingPolicy.from_mapping({"review_step_reserve": 12})

    assert policy.review_step_reserve == 12
    assert RoutingPolicy().review_step_reserve == 5


def test_policy_rejects_invalid_review_step_reserve() -> None:
    with pytest.raises(RoutingConfigError, match="review_step_reserve"):
        RoutingPolicy.from_mapping({"review_step_reserve": -1})
    with pytest.raises(RoutingConfigError, match="review_step_reserve"):
        RoutingPolicy.from_mapping({"review_step_reserve": 51})
    with pytest.raises(RoutingConfigError, match="review_step_reserve"):
        RoutingPolicy.from_mapping({"review_step_reserve": "five"})


def test_review_step_reserve_participates_in_fingerprint() -> None:
    baseline = RoutingPolicy().fingerprint()
    larger = RoutingPolicy.from_mapping({"review_step_reserve": 10}).fingerprint()

    assert larger != baseline
    assert RoutingPolicy.from_mapping({"review_step_reserve": 5}).fingerprint() == baseline
