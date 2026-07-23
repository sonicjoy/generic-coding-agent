"""Deterministic task-complexity assessment for workflow selection."""

from __future__ import annotations

import re
from dataclasses import dataclass

from gca.routing import WORKFLOW_FAST, WORKFLOW_FEATURE, RoutingPolicy

_SCM_ISSUE_TASK_RE = re.compile(
    r"^SCM issue task\..*?\n\nTitle:\s*(.*?)\n\nDescription:\n",
    re.DOTALL | re.IGNORECASE,
)


@dataclass(frozen=True)
class ComplexityAssessment:
    """An auditable complexity result used to select a workflow."""

    score: int
    level: str
    signals: tuple[str, ...]
    recommended_workflow: str


def task_text_for_classification(task: str) -> str:
    """Return the text used for complexity keyword/length scoring.

    SCM issue tasks wrap untrusted title/body with framing boilerplate. Issue
    descriptions often mention process words such as ``end-to-end`` that would
    incorrectly force the feature workflow for tiny labeled tasks. Score those
    runs on the issue title only.
    """

    match = _SCM_ISSUE_TASK_RE.match(task.strip())
    if match is not None:
        title = match.group(1).strip()
        if title:
            return title
    return task


def classify_task(task: str, policy: RoutingPolicy) -> ComplexityAssessment:
    """Classify a task without spending an additional model call."""

    normalized = " ".join(task_text_for_classification(task).lower().split())
    signals: list[str] = []
    score = 0

    small_matches = _matches(normalized, policy.small_keywords)
    if small_matches:
        adjustment = max(-4, -2 * len(small_matches))
        score += adjustment
        signals.append(f"small keywords ({adjustment}): {', '.join(small_matches)}")

    feature_matches = _matches(normalized, policy.feature_keywords)
    if feature_matches:
        adjustment = min(5, 3 + 2 * (len(feature_matches) - 1))
        score += adjustment
        signals.append(f"feature keywords (+{adjustment}): {', '.join(feature_matches)}")

    large_matches = _matches(normalized, policy.large_keywords)
    if large_matches:
        adjustment = min(9, 3 * len(large_matches))
        score += adjustment
        signals.append(f"large-change keywords (+{adjustment}): {', '.join(large_matches)}")

    word_count = len(normalized.split())
    if word_count >= 60:
        score += 2
        signals.append("long task description (+2)")
    elif word_count >= 25:
        score += 1
        signals.append("detailed task description (+1)")

    path_count = len(re.findall(r"(?:^|\s)[\w.-]+/[\w./-]+", normalized))
    if path_count >= 2:
        score += 1
        signals.append("multiple paths mentioned (+1)")

    if large_matches and score < policy.large_threshold:
        score = policy.large_threshold
        signals.append("large-change keyword floor")
    strong_feature_matches = {
        keyword for keyword in feature_matches if keyword not in {"add", "build", "create"}
    }
    if strong_feature_matches and not large_matches and score < policy.feature_threshold:
        score = policy.feature_threshold
        signals.append("explicit feature keyword floor")

    score = max(0, min(10, score))
    if score >= policy.large_threshold:
        level = "large"
        workflow = WORKFLOW_FEATURE
    elif score >= policy.feature_threshold:
        level = "medium"
        workflow = WORKFLOW_FEATURE
    else:
        level = "small"
        workflow = WORKFLOW_FAST

    if not signals:
        signals.append("no complexity signals")
    return ComplexityAssessment(
        score=score,
        level=level,
        signals=tuple(signals),
        recommended_workflow=workflow,
    )


def _matches(text: str, keywords: tuple[str, ...]) -> list[str]:
    matched: list[str] = []
    for keyword in keywords:
        pattern = r"(?<!\w)" + re.escape(keyword).replace(r"\ ", r"\s+") + r"(?!\w)"
        if re.search(pattern, text):
            matched.append(keyword)
    return matched
