"""Workflow selection and sequential multi-agent orchestration."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from gca.agent import Agent, AgentConfig, AgentResult
from gca.complexity import classify_task
from gca.credentials import CredentialBroker
from gca.executor.protocol import CommandExecutor
from gca.models import ModelProfile, ModelRegistry, ModelSelectionError
from gca.personas import PersonaSet
from gca.providers.base import LLMProvider, Message
from gca.providers.fallback import FallbackProvider
from gca.repo_config import RepoConfig
from gca.routing import WORKFLOW_FAST, RoutingPolicy
from gca.session import (
    STATUS_ACTIVE,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PAUSED,
    AgentRunRecord,
    Session,
    SessionStore,
    WorkflowState,
)
from gca.tool_policy import registry_for_phase
from gca.tools.base import ExecutionPolicy, ToolContext, ToolRegistry
from gca.tools.control import FINISH_TOOL_NAME
from gca.workflows import (
    SubmitPlanTool,
    SubmitReviewTool,
    get_workflow,
)

EventHook = Callable[[str], None]

# Steps held back from the implementation phase so review/publish can still run
# within the same budget (or remain available after a mid-implementation pause).
# RoutingPolicy.review_step_reserve overrides this default when configured.
REVIEW_STEP_RESERVE = 5


class _PhaseStore:
    def __init__(
        self,
        parent: Session,
        record: AgentRunRecord,
        model_name: str,
        store: SessionStore,
        *,
        model_role: str,
    ) -> None:
        self.parent = parent
        self.record = record
        self.model_name = model_name
        self.store = store
        self.model_role = model_role

    def save(self, child: Session) -> None:
        self.record.messages = child.messages
        self.record.status = child.status
        self.record.step_count = child.step_count
        self.record.final_message = child.final_message
        self.record.inflight_tool_call_id = child.inflight_tool_call_id
        if child.active_model and child.active_model != self.model_name:
            self.model_name = child.active_model
            self.record.model = child.active_model
            self.parent.active_model = child.active_model
            workflow = self.parent.workflow
            # Only update this phase's role so shared preferences (e.g. planning and
            # review both preferring fable) do not cross-contaminate on failover.
            if workflow is not None:
                workflow.model_bindings[self.model_role] = child.active_model
        workflow = self.parent.workflow
        if workflow is not None:
            workflow.provider_states[self.model_name] = dict(child.provider_state)
        self.parent.step_count = sum(run.step_count for run in self.parent.agent_runs)
        self.store.save(self.parent)


class RunCoordinator:
    """Select a workflow and coordinate one or more role-specific agents."""

    def __init__(
        self,
        *,
        workspace: Path,
        max_steps: int,
        requested_workflow: str | None,
        models: ModelRegistry,
        policy: RoutingPolicy,
        tools: ToolRegistry,
        system_prompt: str,
        repo_config: RepoConfig,
        execution_policy: ExecutionPolicy,
        credentials: CredentialBroker,
        personas: PersonaSet | None = None,
        config_fingerprint: str = "",
        on_event: EventHook | None = None,
        executor: CommandExecutor | None = None,
    ) -> None:
        self.workspace = workspace
        self.max_steps = max_steps
        self.requested_workflow = requested_workflow
        self.models = models
        self.policy = policy
        self.tools = tools
        self.system_prompt = system_prompt
        self.personas = personas or PersonaSet()
        self.config_fingerprint = config_fingerprint
        self.repo_config = repo_config
        self.execution_policy = execution_policy
        self.credentials = credentials
        self.on_event = on_event
        self.executor = executor

    def run(self, session: Session, store: SessionStore) -> AgentResult:
        """Run or resume the selected workflow."""

        if session.status in {STATUS_COMPLETED, STATUS_FAILED}:
            message = session.final_message or f"Session already {session.status}."
            if session.workflow is not None:
                key = "review" if session.status == STATUS_COMPLETED else "error"
                message = session.workflow.artifacts.get(
                    key,
                    session.workflow.artifacts.get("final", message),
                )
            return self._parent_result(session, message)
        if session.workflow is None:
            self._initialize_workflow(session, store)
        else:
            self._check_resume_configuration(session)

        workflow = session.workflow
        if workflow is None:
            raise RuntimeError("workflow initialization failed")
        if workflow.name == WORKFLOW_FAST:
            return self._run_fast(session, store)
        return self._run_feature(session, store)

    def _emit(self, message: str) -> None:
        if self.on_event is not None:
            self.on_event(message)

    def _initialize_workflow(self, session: Session, store: SessionStore) -> None:
        assessment = classify_task(session.task, self.policy)
        if session.messages:
            workflow_name = WORKFLOW_FAST
            assessment_signal = "legacy session history uses fast workflow"
        else:
            workflow_name = self.policy.choose_workflow(
                self.requested_workflow, assessment.recommended_workflow
            )
            assessment_signal = ""

        bindings = self._bind_models(workflow_name, assessment.level)
        first_phase = get_workflow(workflow_name).phases[0].name
        signals = list(assessment.signals)
        if assessment_signal:
            signals.append(assessment_signal)
        session.workflow = WorkflowState(
            name=workflow_name,
            phase=first_phase,
            complexity=assessment.level,
            complexity_score=assessment.score,
            complexity_signals=signals,
            max_review_cycles=self.policy.max_review_cycles,
            model_bindings=bindings,
            registry_fingerprint=self.models.fingerprint(),
            policy_fingerprint=self.policy.fingerprint(),
            config_fingerprint=self.config_fingerprint,
        )
        if workflow_name != WORKFLOW_FAST and not session.messages:
            session.messages.append(Message(role="user", content=session.task))
        session.status = STATUS_ACTIVE
        store.save(session)
        binding_text = ", ".join(f"{role}={name}" for role, name in sorted(bindings.items()))
        self._emit(
            f"[routing] workflow={workflow_name} complexity={assessment.level} "
            f"score={assessment.score} models={binding_text}"
        )

    def _bind_models(self, workflow_name: str, complexity: str) -> dict[str, str]:
        bindings: dict[str, str] = {}
        for phase in get_workflow(workflow_name).phases:
            preferences = self.policy.preferred_models(phase.model_role)
            profile = self.models.select(
                capability=phase.capability,
                strategy=phase.strategy,
                min_strength=self.policy.min_strength(phase.model_role, complexity),
                preferred=preferences or None,
                additional_capabilities=frozenset({"tool_use"}),
            )
            bindings[phase.model_role] = profile.name
        return bindings

    def _provider_for_role(
        self,
        role: str,
        primary: ModelProfile,
        *,
        capability: str,
        min_strength: int,
    ) -> LLMProvider:
        """Return primary provider, wrapped with ordered fallbacks when configured.

        The chain keeps preference order, starts at the currently bound primary
        (so resume after failover does not resurrect an earlier failed model),
        and only includes models that satisfy the same capability/strength rules
        used at bind time.
        """

        chain: list[tuple[str, LLMProvider]] = []
        seen: set[str] = set()
        for name in self.policy.preferred_models(role):
            if name in seen:
                continue
            seen.add(name)
            try:
                profile = self.models.select(
                    capability=capability,
                    strategy="strongest",
                    min_strength=min_strength,
                    preferred=name,
                    additional_capabilities=frozenset({"tool_use"}),
                )
            except (ModelSelectionError, ValueError):
                continue
            chain.append((profile.name, profile.provider))
        names = [name for name, _ in chain]
        if primary.name not in names:
            chain = [(primary.name, primary.provider), *chain]
        else:
            start = names.index(primary.name)
            chain = chain[start:]
        if len(chain) <= 1:
            return primary.provider if not chain else chain[0][1]
        return FallbackProvider(
            chain,
            on_failover=lambda old, new, err: self._emit(
                f"[routing] failover model={old}->{new} reason={err}"
            ),
        )

    def _check_resume_configuration(self, session: Session) -> None:
        workflow = session.workflow
        if workflow is None:
            return
        if (
            workflow.registry_fingerprint
            and workflow.registry_fingerprint != self.models.fingerprint()
        ):
            self._emit("[routing] warning: registered model metadata changed since session start")
        if workflow.policy_fingerprint and workflow.policy_fingerprint != self.policy.fingerprint():
            self._emit("[routing] warning: AGENTS.md routing changed; using saved model bindings")
        if workflow.config_fingerprint and workflow.config_fingerprint != self.config_fingerprint:
            self._emit("[routing] warning: repository configuration changed since session start")
        needed_roles = self._needed_model_roles(workflow)
        for role in needed_roles:
            model_name = workflow.model_bindings[role]
            if self.models.get(model_name) is None:
                raise ValueError(f"saved workflow model is no longer registered: {model_name}")

    @staticmethod
    def _needed_model_roles(workflow: WorkflowState) -> set[str]:
        if workflow.name == WORKFLOW_FAST:
            return {"fast"}
        if workflow.phase == "planning":
            return {"planning", "implementation", "review"}
        if workflow.phase == "implementation":
            return {"implementation", "review"}
        roles = {"review"}
        if workflow.review_cycles < workflow.max_review_cycles:
            roles.add("implementation")
        return roles

    def _run_fast(self, session: Session, store: SessionStore) -> AgentResult:
        workflow = self._workflow(session)
        model_name = workflow.model_bindings["fast"]
        profile = self._model(model_name)
        session.active_model = model_name
        if not session.messages:
            session.messages.append(Message(role="system", content=self.system_prompt))
            session.messages.append(Message(role="user", content=session.task))
        self._emit(f"[routing] phase=execute model={model_name}")
        registry = self._phase_tools("execute", workflow=workflow.name)
        result = Agent(
            provider=self._provider_for_role(
                "fast",
                profile,
                capability="coding",
                min_strength=self.policy.min_strength("fast", workflow.complexity),
            ),
            registry=registry,
            session=session,
            context=self._tool_context("execute", registry),
            store=store,
            config=AgentConfig(max_steps=self.max_steps),
            on_event=self.on_event,
        ).run()
        active = session.active_model or model_name
        workflow.model_bindings["fast"] = active
        workflow.provider_states[active] = dict(session.provider_state)
        workflow.artifacts["final"] = result.final_message
        store.save(session)
        return result

    def _run_feature(self, session: Session, store: SessionStore) -> AgentResult:
        workflow = self._workflow(session)
        while session.step_count < self.max_steps:
            phase = workflow.phase
            spec = next(
                phase_spec
                for phase_spec in get_workflow(workflow.name).phases
                if phase_spec.name == phase
            )
            model_name = workflow.model_bindings[spec.model_role]
            profile = self._model(model_name)
            self._emit(f"[routing] phase={phase} model={model_name}")
            session.status = STATUS_ACTIVE
            steps_before = session.step_count
            result, record = self._run_phase(
                session, store, profile, phase, model_role=spec.model_role
            )

            if result.status == STATUS_PAUSED:
                # Review-only pause with remaining parent budget: auto-resume once
                # the phase store rolled parent.step_count forward so review can
                # spend the held-back reserve instead of stranding a finished impl.
                if (
                    phase == "review"
                    and _implementation_artifact(session)
                    and session.step_count < self.max_steps
                    and session.step_count > steps_before
                ):
                    self._emit(
                        "[routing] review paused with remaining budget; "
                        "auto-resuming review within reserve"
                    )
                    continue
                session.status = STATUS_PAUSED
                session.final_message = result.final_message
                store.save(session)
                return self._parent_result(
                    session,
                    result.final_message,
                    outcome_kind=result.outcome_kind or "budget_exhausted",
                )
            if result.status == STATUS_FAILED:
                return self._fail(session, store, result.final_message)

            if phase == "planning":
                plan = str(_finish_arguments(record).get("plan", result.final_message)).strip()
                if not plan:
                    return self._fail(session, store, "planning agent returned no plan")
                workflow.artifacts["plan"] = plan
                session.plan = plan
                self._audit(session, "planning", plan)
                workflow.phase = "implementation"
            elif phase == "implementation":
                summary = str(
                    _finish_arguments(record).get("summary", result.final_message)
                ).strip()
                if not summary:
                    return self._fail(
                        session,
                        store,
                        "implementation agent returned no summary",
                    )
                workflow.artifacts["implementation"] = summary
                self._audit(session, "implementation", summary)
                workflow.phase = "review"
            else:
                review = _finish_arguments(record)
                verdict = str(review.get("verdict", ""))
                summary = str(review.get("summary", "")).strip()
                if verdict not in {"approved", "changes_requested"}:
                    return self._fail(
                        session,
                        store,
                        "review agent did not return a structured verdict",
                    )
                if not summary:
                    return self._fail(
                        session,
                        store,
                        "review agent returned an empty review summary",
                    )
                workflow.artifacts["review"] = summary
                self._audit(session, "review", f"{verdict}: {summary}")
                if verdict == "approved":
                    session.status = STATUS_COMPLETED
                    session.final_message = summary
                    store.save(session)
                    return self._parent_result(session, summary)
                if workflow.review_cycles >= workflow.max_review_cycles:
                    return self._fail(
                        session,
                        store,
                        f"review still requested changes after "
                        f"{workflow.max_review_cycles} rework cycles: {summary}",
                    )
                workflow.review_cycles += 1
                workflow.artifacts["review_feedback"] = summary
                workflow.phase = "implementation"

            session.status = STATUS_ACTIVE
            store.save(session)

        session.status = STATUS_PAUSED
        message = f"Step budget ({self.max_steps}) exhausted."
        session.final_message = message
        store.save(session)
        return self._parent_result(session, message, outcome_kind="budget_exhausted")

    def _run_phase(
        self,
        parent: Session,
        store: SessionStore,
        profile: ModelProfile,
        phase: str,
        *,
        model_role: str,
    ) -> tuple[AgentResult, AgentRunRecord]:
        workflow = self._workflow(parent)
        parent.active_model = profile.name
        record = self._resumable_record(parent, phase, profile.name)
        if record is not None and record.status == STATUS_COMPLETED:
            final_message = record.final_message
            if not final_message:
                final_message = str(
                    _finish_arguments(record).get(
                        "summary", _finish_arguments(record).get("plan", "")
                    )
                )
            return (
                AgentResult(
                    status=STATUS_COMPLETED,
                    steps=record.step_count,
                    final_message=final_message,
                ),
                record,
            )
        if record is None:
            messages = [
                Message(role="system", content=self._phase_system_prompt(phase)),
                Message(role="user", content=self._phase_user_prompt(parent, phase)),
            ]
            record = AgentRunRecord(phase=phase, model=profile.name, messages=messages)
            parent.agent_runs.append(record)

        child = Session(
            id=f"{parent.id}-{len(parent.agent_runs)}",
            task=parent.task,
            messages=record.messages,
            status=STATUS_ACTIVE,
            step_count=record.step_count,
            provider_state=dict(workflow.provider_states.get(profile.name, {})),
            active_model=profile.name,
            final_message=record.final_message,
            inflight_tool_call_id=record.inflight_tool_call_id,
        )
        remaining = self.max_steps - parent.step_count
        # Cap implementation only: leave a small allotment for review when the
        # overall budget is large enough that the reserve is meaningful.
        reserve = self.policy.review_step_reserve
        if reserve < 0:
            reserve = REVIEW_STEP_RESERVE
        if (
            phase == "implementation"
            and workflow.name != WORKFLOW_FAST
            and self.max_steps > reserve
        ):
            remaining = max(1, remaining - reserve)
        phase_limit = child.step_count + remaining
        phase_store = _PhaseStore(parent, record, profile.name, store, model_role=model_role)
        registry = self._phase_tools(phase, workflow=workflow.name)
        phase_spec = next(item for item in get_workflow(workflow.name).phases if item.name == phase)
        result = Agent(
            provider=self._provider_for_role(
                model_role,
                profile,
                capability=phase_spec.capability,
                min_strength=self.policy.min_strength(model_role, workflow.complexity),
            ),
            registry=registry,
            session=child,
            context=self._tool_context(phase, registry),
            store=phase_store,
            config=AgentConfig(max_steps=phase_limit),
            on_event=self.on_event,
        ).run()
        record.final_message = result.final_message
        phase_store.save(child)
        return result, record

    def _phase_tools(self, phase: str, *, workflow: str = "feature") -> ToolRegistry:
        registry = registry_for_phase(
            self.tools,
            self.repo_config,
            phase,
            workflow=workflow,
        )
        if phase == "planning":
            registry.register(SubmitPlanTool())
        elif phase == "review":
            registry.register(SubmitReviewTool())
        return registry

    def _tool_context(self, phase: str, registry: ToolRegistry) -> ToolContext:
        return ToolContext(
            workspace=self.workspace,
            phase=phase,
            audit_id=phase,
            allowed_tools=frozenset(registry.names()),
            tool_secret_access=self.repo_config.tools.secret_access,
            execution=self.execution_policy,
            credentials=self.credentials,
            executor=self.executor,
        )

    def _phase_system_prompt(self, phase: str) -> str:
        role = self.personas.for_phase(phase)
        return f"{self.system_prompt}\n\nWorkflow role:\n{role}"

    def _phase_user_prompt(self, session: Session, phase: str) -> str:
        workflow = self._workflow(session)
        parts = [f"Task:\n{session.task}"]
        plan = workflow.artifacts.get("plan")
        if phase != "planning" and plan:
            parts.append(f"Approved plan:\n{plan}")
        if phase == "implementation":
            feedback = workflow.artifacts.get("review_feedback")
            if feedback:
                parts.append(f"Review feedback to resolve:\n{feedback}")
        if phase == "review":
            implementation = workflow.artifacts.get("implementation", "")
            parts.append(f"Implementation summary:\n{implementation}")
            feedback = workflow.artifacts.get("review_feedback")
            if feedback:
                parts.append(f"Previous review feedback:\n{feedback}")
        return "\n\n".join(parts)

    @staticmethod
    def _resumable_record(session: Session, phase: str, model_name: str) -> AgentRunRecord | None:
        if not session.agent_runs:
            return None
        record = session.agent_runs[-1]
        if (
            record.phase == phase
            and record.model == model_name
            and record.status in {STATUS_ACTIVE, STATUS_COMPLETED, STATUS_PAUSED}
        ):
            return record
        return None

    @staticmethod
    def _audit(session: Session, phase: str, summary: str) -> None:
        session.messages.append(Message(role="assistant", content=f"[{phase}] {summary}"))

    def _model(self, name: str) -> ModelProfile:
        profile = self.models.get(name)
        if profile is None:
            raise ValueError(f"workflow model is not registered: {name}")
        return profile

    @staticmethod
    def _workflow(session: Session) -> WorkflowState:
        if session.workflow is None:
            raise RuntimeError("session has no workflow state")
        return session.workflow

    @staticmethod
    def _parent_result(
        session: Session,
        message: str,
        *,
        outcome_kind: str | None = None,
    ) -> AgentResult:
        kind = outcome_kind
        if kind is None and session.status == STATUS_PAUSED:
            kind = "budget_exhausted"
        return AgentResult(
            status=session.status,
            steps=session.step_count,
            final_message=message,
            outcome_kind=kind,
        )

    def _fail(self, session: Session, store: SessionStore, message: str) -> AgentResult:
        session.status = STATUS_FAILED
        session.final_message = message
        if session.workflow is not None:
            session.workflow.artifacts["error"] = message
        store.save(session)
        return self._parent_result(session, message, outcome_kind="failed")


def _implementation_artifact(session: Session) -> str | None:
    """Return a non-empty implementation summary if the workflow recorded one."""

    workflow = session.workflow
    if workflow is None:
        return None
    summary = str(workflow.artifacts.get("implementation") or "").strip()
    return summary or None


def _finish_arguments(record: AgentRunRecord) -> dict[str, object]:
    for message in reversed(record.messages):
        if message.role != "assistant":
            continue
        for call in reversed(message.tool_calls):
            if call.name == FINISH_TOOL_NAME:
                return dict(call.arguments)
    return {}
