"""Validated routing policy loaded from ``AGENTS.md`` configuration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

WORKFLOW_AUTO = "auto"
WORKFLOW_FAST = "fast"
WORKFLOW_FEATURE = "feature"
WORKFLOWS = frozenset({WORKFLOW_AUTO, WORKFLOW_FAST, WORKFLOW_FEATURE})
MODEL_ROLES = frozenset({"fast", "planning", "implementation", "review"})

DEFAULT_SMALL_KEYWORDS = (
    "comment",
    "documentation",
    "format",
    "lint",
    "spelling",
    "typo",
)
DEFAULT_FEATURE_KEYWORDS = (
    "add",
    "build",
    "create",
    "enable",
    "implement",
    "introduce",
    "new feature",
    "support",
)
DEFAULT_LARGE_KEYWORDS = (
    "architecture",
    "authentication",
    "database",
    "end-to-end",
    "migrate",
    "multi-agent",
    "multiple providers",
    "redesign",
    "refactor",
    "security",
    "workflow",
)


class RoutingConfigError(ValueError):
    """Raised when GCA routing configuration is invalid."""


@dataclass(frozen=True)
class RoutingPolicy:
    """Model and workflow preferences for a run."""

    workflow: str = WORKFLOW_AUTO
    model_preferences: dict[str, str] = field(default_factory=dict)
    minimum_strength: dict[str, int] = field(default_factory=dict)
    max_review_cycles: int = 2
    feature_threshold: int = 3
    large_threshold: int = 6
    small_keywords: tuple[str, ...] = DEFAULT_SMALL_KEYWORDS
    feature_keywords: tuple[str, ...] = DEFAULT_FEATURE_KEYWORDS
    large_keywords: tuple[str, ...] = DEFAULT_LARGE_KEYWORDS

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> RoutingPolicy:
        """Validate and construct a policy from merged ``gca`` frontmatter."""

        raw = dict(value or {})
        allowed = {
            "workflow",
            "models",
            "minimum_strength",
            "max_review_cycles",
            "complexity",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise RoutingConfigError(f"unknown gca configuration keys: {', '.join(unknown)}")

        workflow = str(raw.get("workflow", WORKFLOW_AUTO))
        if workflow not in WORKFLOWS:
            raise RoutingConfigError(
                f"workflow must be one of: {', '.join(sorted(WORKFLOWS))}"
            )

        model_preferences = _string_mapping(raw.get("models", {}), "models")
        _validate_role_keys(model_preferences, "models")

        minimum_strength = _score_mapping(
            raw.get("minimum_strength", {}), "minimum_strength"
        )
        _validate_role_keys(minimum_strength, "minimum_strength")

        max_review_cycles = _integer(
            raw.get("max_review_cycles", 2), "max_review_cycles", minimum=0, maximum=10
        )

        complexity = raw.get("complexity", {})
        if not isinstance(complexity, Mapping):
            raise RoutingConfigError("complexity must be a mapping")
        complexity_raw = dict(complexity)
        allowed_complexity = {
            "feature_threshold",
            "large_threshold",
            "small_keywords",
            "feature_keywords",
            "large_keywords",
        }
        unknown_complexity = sorted(set(complexity_raw) - allowed_complexity)
        if unknown_complexity:
            raise RoutingConfigError(
                f"unknown complexity keys: {', '.join(unknown_complexity)}"
            )

        feature_threshold = _integer(
            complexity_raw.get("feature_threshold", 3),
            "complexity.feature_threshold",
            minimum=0,
            maximum=10,
        )
        large_threshold = _integer(
            complexity_raw.get("large_threshold", 6),
            "complexity.large_threshold",
            minimum=0,
            maximum=10,
        )
        if feature_threshold > large_threshold:
            raise RoutingConfigError(
                "complexity.feature_threshold must not exceed large_threshold"
            )

        return cls(
            workflow=workflow,
            model_preferences=model_preferences,
            minimum_strength=minimum_strength,
            max_review_cycles=max_review_cycles,
            feature_threshold=feature_threshold,
            large_threshold=large_threshold,
            small_keywords=_keywords(
                complexity_raw.get("small_keywords"), DEFAULT_SMALL_KEYWORDS
            ),
            feature_keywords=_keywords(
                complexity_raw.get("feature_keywords"), DEFAULT_FEATURE_KEYWORDS
            ),
            large_keywords=_keywords(
                complexity_raw.get("large_keywords"), DEFAULT_LARGE_KEYWORDS
            ),
        )

    def preferred_model(self, role: str) -> str | None:
        """Return an explicit model preference for ``role``, if configured."""

        return self.model_preferences.get(role)

    def min_strength(self, role: str, complexity: str) -> int:
        """Return the minimum model strength for a role and task complexity."""

        configured = self.minimum_strength.get(role, 1)
        if role == "implementation" and complexity == "large":
            return max(configured, 3)
        return configured

    def choose_workflow(self, cli_workflow: str | None, recommended: str) -> str:
        """Resolve workflow precedence: CLI, ``AGENTS.md``, then classifier."""

        if cli_workflow is not None:
            return recommended if cli_workflow == WORKFLOW_AUTO else cli_workflow
        if self.workflow != WORKFLOW_AUTO:
            return self.workflow
        return recommended

    def fingerprint(self) -> str:
        """Return a stable policy fingerprint for resume diagnostics."""

        payload = {
            "workflow": self.workflow,
            "models": self.model_preferences,
            "minimum_strength": self.minimum_strength,
            "max_review_cycles": self.max_review_cycles,
            "feature_threshold": self.feature_threshold,
            "large_threshold": self.large_threshold,
            "small_keywords": self.small_keywords,
            "feature_keywords": self.feature_keywords,
            "large_keywords": self.large_keywords,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


def _integer(value: object, path: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RoutingConfigError(f"{path} must be an integer")
    if not minimum <= value <= maximum:
        raise RoutingConfigError(f"{path} must be from {minimum} to {maximum}")
    return value


def _string_mapping(value: object, path: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise RoutingConfigError(f"{path} must be a mapping")
    result: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise RoutingConfigError(f"{path}.{key} must be a non-empty string")
        result[key] = raw_value
    return result


def _score_mapping(value: object, path: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise RoutingConfigError(f"{path} must be a mapping")
    return {
        str(key): _integer(score, f"{path}.{key}", minimum=1, maximum=5)
        for key, score in value.items()
    }


def _validate_role_keys(value: Mapping[str, object], path: str) -> None:
    unknown = sorted(set(value) - MODEL_ROLES)
    if unknown:
        raise RoutingConfigError(f"unknown {path} roles: {', '.join(unknown)}")


def _keywords(value: object, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if not isinstance(value, list) or not all(
        isinstance(keyword, str) and keyword.strip() for keyword in value
    ):
        raise RoutingConfigError("complexity keyword values must be lists of strings")
    return tuple(keyword.lower() for keyword in value)
