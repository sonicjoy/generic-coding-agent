from __future__ import annotations

from pathlib import Path

from gca.credentials import CredentialBroker
from gca.models import ModelProfile, ModelRegistry
from gca.orchestrator import RunCoordinator, _PhaseStore
from gca.providers.base import LLMProvider, LLMResponse, Message, ToolSpec
from gca.providers.fallback import FallbackProvider
from gca.repo_config import load_repo_config
from gca.routing import RoutingPolicy
from gca.session import AgentRunRecord, Session, WorkflowState
from gca.tools.base import ExecutionPolicy, ToolRegistry


class StubProvider(LLMProvider):
    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        return LLMResponse(content="ok")


class MemoryStore:
    def __init__(self) -> None:
        self.saved: list[Session] = []

    def save(self, session: Session) -> None:
        self.saved.append(session)


def _coordinator(tmp_path: Path, policy: RoutingPolicy, models: ModelRegistry) -> RunCoordinator:
    (tmp_path / ".gca").mkdir(exist_ok=True)
    repo_config = load_repo_config(tmp_path)
    return RunCoordinator(
        workspace=tmp_path,
        max_steps=10,
        requested_workflow=None,
        models=models,
        policy=policy,
        tools=ToolRegistry(),
        system_prompt="test",
        repo_config=repo_config,
        execution_policy=ExecutionPolicy(),
        credentials=CredentialBroker(),
    )


def test_provider_chain_starts_at_bound_primary(tmp_path: Path) -> None:
    registry = ModelRegistry()
    fable = StubProvider()
    opus = StubProvider()
    registry.register(ModelProfile("claude-fable-5", fable, strength=5))
    registry.register(ModelProfile("claude-opus-4.8", opus, strength=5))
    policy = RoutingPolicy.from_mapping(
        {"models": {"planning": ["claude-fable-5", "claude-opus-4.8"]}}
    )
    coordinator = _coordinator(tmp_path, policy, registry)
    primary = registry.get("claude-opus-4.8")
    assert primary is not None

    provider = coordinator._provider_for_role(  # noqa: SLF001
        "planning",
        primary,
        capability="planning",
        min_strength=1,
    )

    # After failover binding to opus, chain must not resurrect fable first.
    assert not isinstance(provider, FallbackProvider)
    assert provider is opus


def test_phase_store_failover_updates_only_active_role(tmp_path: Path) -> None:
    parent = Session(
        id="parent",
        task="task",
        workflow=WorkflowState(
            name="feature",
            phase="planning",
            model_bindings={
                "planning": "claude-fable-5",
                "implementation": "grok-4.5",
                "review": "claude-fable-5",
            },
        ),
    )
    record = AgentRunRecord(phase="planning", model="claude-fable-5")
    store = MemoryStore()
    phase_store = _PhaseStore(parent, record, "claude-fable-5", store, model_role="planning")
    child = Session(id="child", task="task", active_model="claude-opus-4.8")

    phase_store.save(child)

    assert parent.workflow is not None
    assert parent.workflow.model_bindings["planning"] == "claude-opus-4.8"
    assert parent.workflow.model_bindings["review"] == "claude-fable-5"
    assert record.model == "claude-opus-4.8"
