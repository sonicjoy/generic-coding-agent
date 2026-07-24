"""Aggregate LLM token and cost usage across provider calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMUsage:
    """Token/cost metrics from one provider completion."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    model: str = ""
    generation_id: str = ""


@dataclass
class LLMUsageTotals:
    """Cumulative usage for a session or job."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    by_model: dict[str, dict[str, float]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize totals for session/job persistence and APIs."""

        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "by_model": {
                model: {
                    "prompt_tokens": int(values.get("prompt_tokens", 0)),
                    "completion_tokens": int(values.get("completion_tokens", 0)),
                    "total_tokens": int(values.get("total_tokens", 0)),
                    "cost_usd": float(values.get("cost_usd", 0.0)),
                }
                for model, values in sorted(self.by_model.items())
            },
        }


def empty_usage_dict() -> dict[str, Any]:
    """Return an empty serializable usage payload."""

    return LLMUsageTotals().to_dict()


def totals_from_dict(data: dict[str, Any] | None) -> LLMUsageTotals:
    """Load cumulative usage from persisted JSON."""

    raw = dict(data or {})
    by_model_raw = raw.get("by_model") or {}
    by_model: dict[str, dict[str, float]] = {}
    if isinstance(by_model_raw, dict):
        for model, values in by_model_raw.items():
            if not isinstance(values, dict):
                continue
            by_model[str(model)] = {
                "prompt_tokens": float(values.get("prompt_tokens", 0) or 0),
                "completion_tokens": float(values.get("completion_tokens", 0) or 0),
                "total_tokens": float(values.get("total_tokens", 0) or 0),
                "cost_usd": float(values.get("cost_usd", 0) or 0),
            }
    return LLMUsageTotals(
        prompt_tokens=int(raw.get("prompt_tokens", 0) or 0),
        completion_tokens=int(raw.get("completion_tokens", 0) or 0),
        total_tokens=int(raw.get("total_tokens", 0) or 0),
        cost_usd=float(raw.get("cost_usd", 0) or 0),
        by_model=by_model,
    )


def merge_usage(totals: LLMUsageTotals, usage: LLMUsage | None) -> LLMUsageTotals:
    """Add one completion's usage into cumulative totals."""

    if usage is None:
        return totals
    prompt = max(0, int(usage.prompt_tokens))
    completion = max(0, int(usage.completion_tokens))
    total = max(0, int(usage.total_tokens) or (prompt + completion))
    cost = float(usage.cost_usd or 0.0)
    model = (usage.model or "unknown").strip() or "unknown"
    bucket = totals.by_model.setdefault(
        model,
        {"prompt_tokens": 0.0, "completion_tokens": 0.0, "total_tokens": 0.0, "cost_usd": 0.0},
    )
    bucket["prompt_tokens"] += prompt
    bucket["completion_tokens"] += completion
    bucket["total_tokens"] += total
    bucket["cost_usd"] += cost
    return LLMUsageTotals(
        prompt_tokens=totals.prompt_tokens + prompt,
        completion_tokens=totals.completion_tokens + completion,
        total_tokens=totals.total_tokens + total,
        cost_usd=totals.cost_usd + cost,
        by_model=totals.by_model,
    )


def merge_totals(left: LLMUsageTotals, right: LLMUsageTotals) -> LLMUsageTotals:
    """Combine two cumulative usage totals."""

    merged = LLMUsageTotals(
        prompt_tokens=left.prompt_tokens + right.prompt_tokens,
        completion_tokens=left.completion_tokens + right.completion_tokens,
        total_tokens=left.total_tokens + right.total_tokens,
        cost_usd=left.cost_usd + right.cost_usd,
        by_model={key: dict(value) for key, value in left.by_model.items()},
    )
    for model, values in right.by_model.items():
        bucket = merged.by_model.setdefault(
            model,
            {"prompt_tokens": 0.0, "completion_tokens": 0.0, "total_tokens": 0.0, "cost_usd": 0.0},
        )
        bucket["prompt_tokens"] += float(values.get("prompt_tokens", 0) or 0)
        bucket["completion_tokens"] += float(values.get("completion_tokens", 0) or 0)
        bucket["total_tokens"] += float(values.get("total_tokens", 0) or 0)
        bucket["cost_usd"] += float(values.get("cost_usd", 0) or 0)
    return merged
