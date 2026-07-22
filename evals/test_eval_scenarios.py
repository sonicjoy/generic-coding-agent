"""Pytest entrypoint for offline eval scenarios."""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.harness import discover_scenarios, run_scenario

SCENARIOS = discover_scenarios()


@pytest.mark.eval
@pytest.mark.parametrize("scenario", SCENARIOS, ids=[scenario.id for scenario in SCENARIOS])
def test_eval_scenario(scenario: object, tmp_path: Path) -> None:
    result = run_scenario(scenario, tmp_path / scenario.id)
    failed = [check for check in result.checks if not check.passed]
    assert result.passed, (
        f"{scenario.id} failed {len(failed)}/{len(result.checks)} checks: "
        + "; ".join(f"{check.name}: {check.detail}" for check in failed)
    )
