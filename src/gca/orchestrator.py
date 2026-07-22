"""Workflow selection and sequential multi-agent orchestration."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from gca.agent import Agent, AgentConfig, AgentResult
from gca.complexity import classify_task
from gca.models import ModelProfile, ModelRegistry
from gca.personas import PersonaSet
from gca.providers.base import Message
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
from gca.tools.base import ToolContext, ToolRegistry
from gca.tools.control import FINISH_TOOL_NAME
from gca.workflows import (
    SubmitPlanTool,
    SubmitReviewTool,
    get_workflow,
)

EventHook = Callable[[str], None]


class _PhaseStore:
    def __init__(
        self,
        parent: Session,
        record: AgentRunRecord,
        model_name: str,
        store: SessionStore,
    ) -> None:
        self.parent = parent
        self.record = record
        self.model_name = model_name
        self.store = store

    def save(self, child: Session) -> None:
        self.record.messages = child.messages
        self.record.status = child.status
        self.record.step_count = child.step_count
        self.record.final_message = child.final_message
        self.record.inflight_tool_call_id = child.inflight_tool_call_id
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
        personas: PersonaSet | None = None,
        config_fingerprint: str = "",
        on_event: EventHook | None = None,
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
        self.on_event = on_event

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
            profile = self.models.select(
                capability=phase.capability,
                strategy=phase.strategy,
                min_strength=self.policy.min_strength(phase.model_role, complexity),
                preferred=self.policy.preferred_model(phase.model_role),
                additional_capabilities=frozenset({"tool_use"}),
            )
            bindings[phase.model_role] = profile.name
        return bindings

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
        result = Agent(
            provider=profile.provider,
            registry=self.tools,
            session=session,
            context=ToolContext(workspace=self.workspace),
            store=store,
            config=AgentConfig(max_steps=self.max_steps),
            on_event=self.on_event,
        ).run()
        workflow.provider_states[model_name] = dict(session.provider_state)
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
            result, record = self._run_phase(session, store, profile, phase)

            if result.status == STATUS_PAUSED:
                session.status = STATUS_PAUSED
                session.final_message = result.final_message
                store.save(session)
                return self._parent_result(session, result.final_message)
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
        return self._parent_result(session, message)

    def _run_phase(
        self,
        parent: Session,
        store: SessionStore,
        profile: ModelProfile,
        phase: str,
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
        phase_limit = child.step_count + remaining
        phase_store = _PhaseStore(parent, record, profile.name, store)
        result = Agent(
            provider=profile.provider,
            registry=self._phase_tools(phase),
            session=child,
            context=ToolContext(workspace=self.workspace),
            store=phase_store,
            config=AgentConfig(max_steps=phase_limit),
            on_event=self.on_event,
        ).run()
        record.final_message = result.final_message
        phase_store.save(child)
        return result, record

    def _phase_tools(self, phase: str) -> ToolRegistry:
        spec = next(
            phase_spec for phase_spec in get_workflow("feature").phases if phase_spec.name == phase
        )
        names = set(self.tools.names()) if spec.allowed_tools is None else set(spec.allowed_tools)
        names.add(FINISH_TOOL_NAME)
        registry = self.tools.subset(names)
        if phase == "planning":
            registry.register(SubmitPlanTool())
        elif phase == "review":
            registry.register(SubmitReviewTool())
        return registry

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
    def _parent_result(session: Session, message: str) -> AgentResult:
        return AgentResult(
            status=session.status,
            steps=session.step_count,
            final_message=message,
        )

    def _fail(self, session: Session, store: SessionStore, message: str) -> AgentResult:
        session.status = STATUS_FAILED
        session.final_message = message
        if session.workflow is not None:
            session.workflow.artifacts["error"] = message
        store.save(session)
        return self._parent_result(session, message)


def _finish_arguments(record: AgentRunRecord) -> dict[str, object]:
    for message in reversed(record.messages):
        if message.role != "assistant":
            continue
        for call in reversed(message.tool_calls):
            if call.name == FINISH_TOOL_NAME:
                return dict(call.arguments)
    return {}
