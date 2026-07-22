"""Offline evaluation harness for deterministic workflow scenarios.

Scenarios live as YAML under ``evals/scenarios/``. Each scenario drives
scripted models through ``create_coordinator`` and scores observable outcomes
such as workflow selection, phase order, model bindings, artifacts, and files.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from gca.models import ModelProfile, ModelRegistry
from gca.providers.scripted import ScriptedProvider
from gca.runtime import RuntimeConfig, create_coordinator
from gca.session import Session, SessionStore

SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"


@dataclass(frozen=True)
class EvalCheck:
    """One scored assertion against an eval run."""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class EvalResult:
    """Aggregate score for one scenario."""

    scenario_id: str
    passed: bool
    checks: list[EvalCheck] = field(default_factory=list)
    status: str = ""
    workflow: str = ""
    steps: int = 0

    @property
    def score(self) -> float:
        if not self.checks:
            return 0.0
        return sum(1 for check in self.checks if check.passed) / len(self.checks)


@dataclass(frozen=True)
class EvalScenario:
    """A declarative offline eval case."""

    id: str
    task: str
    path: Path
    description: str = ""
    workflow: str | None = None
    max_steps: int = 25
    resume_max_steps: int | None = None
    agents_md: str | None = None
    models_yaml: str | None = None
    scripts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    model_profiles: list[dict[str, Any]] = field(default_factory=list)
    expect: dict[str, Any] = field(default_factory=dict)


def discover_scenarios(root: Path | None = None) -> list[EvalScenario]:
    """Load all ``*.yaml`` scenarios under ``root``."""

    root = root or SCENARIOS_DIR
    scenarios: list[EvalScenario] = []
    for path in sorted(root.glob("*.yaml")):
        scenarios.append(load_scenario(path))
    return scenarios


def load_scenario(path: Path) -> EvalScenario:
    """Parse one scenario YAML file."""

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"scenario {path} must be a mapping")
    scenario_id = str(raw.get("id") or path.stem)
    task = raw.get("task")
    if not isinstance(task, str) or not task.strip():
        raise ValueError(f"scenario {path} requires a task string")
    scripts = raw.get("scripts", {})
    if not isinstance(scripts, Mapping):
        raise ValueError(f"scenario {path} scripts must be a mapping")
    profiles = raw.get("model_profiles", [])
    if not isinstance(profiles, list):
        raise ValueError(f"scenario {path} model_profiles must be a list")
    expect = raw.get("expect", {})
    if not isinstance(expect, Mapping):
        raise ValueError(f"scenario {path} expect must be a mapping")
    return EvalScenario(
        id=scenario_id,
        task=task,
        path=path,
        description=str(raw.get("description", "")),
        workflow=raw.get("workflow"),
        max_steps=int(raw.get("max_steps", 25)),
        resume_max_steps=(
            int(raw["resume_max_steps"]) if raw.get("resume_max_steps") is not None else None
        ),
        agents_md=raw.get("agents_md"),
        models_yaml=raw.get("models_yaml"),
        scripts={str(name): list(steps) for name, steps in scripts.items()},
        model_profiles=[dict(profile) for profile in profiles],
        expect=dict(expect),
    )


def run_scenario(scenario: EvalScenario, workspace: Path) -> EvalResult:
    """Execute ``scenario`` in ``workspace`` and score expectations."""

    workspace.mkdir(parents=True, exist_ok=True)
    if scenario.agents_md:
        (workspace / "AGENTS.md").write_text(scenario.agents_md, encoding="utf-8")
    if scenario.models_yaml:
        (workspace / "models.yaml").write_text(scenario.models_yaml, encoding="utf-8")

    sessions_dir = workspace / ".gca" / "sessions"
    store = SessionStore(sessions_dir)
    session = store.create(scenario.task)
    registry = _build_registry(scenario)
    config = RuntimeConfig(
        workspace=workspace,
        sessions_dir=sessions_dir,
        max_steps=scenario.max_steps,
        workflow=scenario.workflow,
    )
    result = create_coordinator(config, registry).run(session, store)

    if scenario.resume_max_steps is not None and result.status == "paused":
        reloaded = store.load(session.id)
        resume_config = RuntimeConfig(
            workspace=workspace,
            sessions_dir=sessions_dir,
            max_steps=scenario.resume_max_steps,
            workflow=scenario.workflow,
        )
        registry = _build_registry(scenario)
        result = create_coordinator(resume_config, registry).run(reloaded, store)
        session = reloaded

    checks = _score(scenario, workspace, session, result.status, result.steps, result.final_message)
    return EvalResult(
        scenario_id=scenario.id,
        passed=all(check.passed for check in checks),
        checks=checks,
        status=result.status,
        workflow=session.workflow.name if session.workflow is not None else "",
        steps=result.steps,
    )


def _build_registry(scenario: EvalScenario) -> ModelRegistry:
    providers = {
        name: ScriptedProvider.from_script(steps) for name, steps in scenario.scripts.items()
    }
    registry = ModelRegistry()
    if scenario.model_profiles:
        for profile in scenario.model_profiles:
            name = str(profile["name"])
            script = str(profile.get("script", name))
            if script not in providers:
                raise ValueError(f"scenario {scenario.id} missing script '{script}'")
            registry.register(
                ModelProfile(
                    name=name,
                    provider=providers[script],
                    strength=int(profile.get("strength", 3)),
                    speed=int(profile.get("speed", 3)),
                    cost=int(profile.get("cost", 3)),
                    model_id=str(profile.get("model_id", name)),
                )
            )
        return registry

    if "fast" not in providers or "strong" not in providers:
        raise ValueError(
            f"scenario {scenario.id} needs fast/strong scripts or explicit model_profiles"
        )
    registry.register(ModelProfile("fast", providers["fast"], strength=2, speed=5, cost=1))
    registry.register(ModelProfile("strong", providers["strong"], strength=5, speed=2, cost=5))
    return registry


def _score(
    scenario: EvalScenario,
    workspace: Path,
    session: Session,
    status: str,
    steps: int,
    final_message: str,
) -> list[EvalCheck]:
    expect = scenario.expect
    checks: list[EvalCheck] = []

    if "status" in expect:
        wanted = str(expect["status"])
        checks.append(
            EvalCheck(
                "status",
                status == wanted,
                f"expected {wanted!r}, got {status!r}",
            )
        )

    workflow = session.workflow
    if "workflow" in expect:
        wanted = str(expect["workflow"])
        actual = workflow.name if workflow is not None else ""
        checks.append(
            EvalCheck(
                "workflow",
                actual == wanted,
                f"expected {wanted!r}, got {actual!r}",
            )
        )

    if "complexity" in expect:
        wanted = str(expect["complexity"])
        actual = workflow.complexity if workflow is not None else ""
        checks.append(
            EvalCheck(
                "complexity",
                actual == wanted,
                f"expected {wanted!r}, got {actual!r}",
            )
        )

    if "phases" in expect:
        wanted = [str(item) for item in expect["phases"]]
        actual = [run.phase for run in session.agent_runs]
        checks.append(
            EvalCheck(
                "phases",
                actual == wanted,
                f"expected {wanted!r}, got {actual!r}",
            )
        )

    if "models" in expect:
        wanted = [str(item) for item in expect["models"]]
        actual = [run.model for run in session.agent_runs]
        checks.append(
            EvalCheck(
                "models",
                actual == wanted,
                f"expected {wanted!r}, got {actual!r}",
            )
        )

    if "model_bindings" in expect:
        wanted = {str(key): str(value) for key, value in dict(expect["model_bindings"]).items()}
        actual = dict(workflow.model_bindings) if workflow is not None else {}
        checks.append(
            EvalCheck(
                "model_bindings",
                actual == wanted,
                f"expected {wanted!r}, got {actual!r}",
            )
        )

    if "active_model" in expect:
        wanted = str(expect["active_model"])
        checks.append(
            EvalCheck(
                "active_model",
                session.active_model == wanted,
                f"expected {wanted!r}, got {session.active_model!r}",
            )
        )

    if "review_cycles" in expect:
        wanted = int(expect["review_cycles"])
        actual = workflow.review_cycles if workflow is not None else -1
        checks.append(
            EvalCheck(
                "review_cycles",
                actual == wanted,
                f"expected {wanted}, got {actual}",
            )
        )

    if "plan_contains" in expect:
        needle = str(expect["plan_contains"])
        checks.append(
            EvalCheck(
                "plan_contains",
                needle in session.plan,
                f"plan missing {needle!r}",
            )
        )

    if "final_contains" in expect:
        needle = str(expect["final_contains"])
        checks.append(
            EvalCheck(
                "final_contains",
                needle in final_message,
                f"final message missing {needle!r}",
            )
        )

    if "max_steps" in expect:
        limit = int(expect["max_steps"])
        checks.append(
            EvalCheck(
                "max_steps",
                steps <= limit,
                f"used {steps} steps, limit {limit}",
            )
        )

    files = expect.get("files", {})
    if isinstance(files, Mapping):
        for relative, rules in files.items():
            path = workspace / str(relative)
            exists = path.is_file()
            checks.append(
                EvalCheck(
                    f"file_exists:{relative}",
                    exists,
                    f"missing file {relative}",
                )
            )
            if not exists or not isinstance(rules, Mapping):
                continue
            text = path.read_text(encoding="utf-8")
            if "equals" in rules:
                wanted = str(rules["equals"])
                checks.append(
                    EvalCheck(
                        f"file_equals:{relative}",
                        text == wanted,
                        f"expected {wanted!r}, got {text!r}",
                    )
                )
            if "contains" in rules:
                needle = str(rules["contains"])
                checks.append(
                    EvalCheck(
                        f"file_contains:{relative}",
                        needle in text,
                        f"{relative} missing {needle!r}",
                    )
                )

    if not checks:
        checks.append(EvalCheck("expect", False, "scenario expect block was empty"))
    return checks
