"""Pytest entrypoint for offline eval scenarios."""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.harness import EvalScenario, discover_scenarios, run_scenario

SCENARIOS = discover_scenarios()


@pytest.mark.eval
@pytest.mark.parametrize("scenario", SCENARIOS, ids=[scenario.id for scenario in SCENARIOS])
def test_eval_scenario(scenario: EvalScenario, tmp_path: Path) -> None:
    result = run_scenario(scenario, tmp_path / scenario.id)
    failed = [check for check in result.checks if not check.passed]
    assert result.passed, (
        f"{scenario.id} failed {len(failed)}/{len(result.checks)} checks: "
        + "; ".join(f"{check.name}: {check.detail}" for check in failed)
    )


@pytest.mark.eval
def test_harness_detects_failing_expectations(tmp_path: Path) -> None:
    """Guard the gate itself: wrong expectations must fail, not silently pass."""

    scenario = EvalScenario(
        id="meta_failing",
        task="Fix a typo in README.md",
        path=tmp_path / "meta_failing.yaml",
        scripts={
            "fast": [{"tool_calls": [{"name": "finish", "arguments": {"summary": "Fixed."}}]}],
            "strong": [],
        },
        expect={
            "status": "failed",  # actual run completes
            "workflow": "feature",  # actual workflow is fast
            "script_calls": {"strong": 3},  # strong is never called
            "files": {"missing.txt": {"equals": "nope"}},
        },
    )

    result = run_scenario(scenario, tmp_path / "ws")

    assert not result.passed
    failed_names = {check.name for check in result.checks if not check.passed}
    assert "status" in failed_names
    assert "workflow" in failed_names
    assert "script_calls:strong" in failed_names
    assert "file_exists:missing.txt" in failed_names


@pytest.mark.eval
def test_harness_detects_unconsumed_scripts(tmp_path: Path) -> None:
    """Leftover scripted steps (a skipped phase) must fail the scenario."""

    scenario = EvalScenario(
        id="meta_unconsumed",
        task="Fix a typo in README.md",
        path=tmp_path / "meta_unconsumed.yaml",
        scripts={
            "fast": [
                {"tool_calls": [{"name": "finish", "arguments": {"summary": "Fixed."}}]},
                {"tool_calls": [{"name": "finish", "arguments": {"summary": "Never runs."}}]},
            ],
            "strong": [],
        },
        expect={"status": "completed"},
    )

    result = run_scenario(scenario, tmp_path / "ws")

    assert not result.passed
    failed_names = {check.name for check in result.checks if not check.passed}
    assert "script_consumed:fast" in failed_names
