"""Apply structured agent turn outcomes to issue-session state."""

from __future__ import annotations

import json
import uuid

from gca.agent import AgentResult
from gca.issue_sessions.models import (
    GenerationStatus,
    OutboundAction,
    Turn,
    TurnOutcomeKind,
    TurnStatus,
    WaitReason,
)
from gca.issue_sessions.store import IssueSessionStore
from gca.session import STATUS_FAILED, STATUS_PAUSED


class TurnOutcomeApplicator:
    """Map one AgentResult onto generation/turn/outbox state."""

    def __init__(self, store: IssueSessionStore) -> None:
        self.store = store

    def apply(self, *, turn_id: str, result: AgentResult) -> None:
        """Persist turn completion and schedule follow-up service actions."""

        with self.store.unit_of_work() as uow:
            row = uow.connection.execute(
                "SELECT data FROM issue_turns WHERE id = ?",
                (turn_id,),
            ).fetchone()
            turn = Turn.from_dict(json.loads(str(row["data"])))
            generation = uow.get_generation(turn.generation_id)
            session = uow.get_session(turn.issue_session_id)
            outcome = _outcome_kind(result)
            turn.steps_consumed = result.steps
            turn.outcome_kind = outcome
            turn.outcome_summary = result.final_message
            generation.steps_consumed += max(0, result.steps)

            if outcome == TurnOutcomeKind.NEEDS_HUMAN:
                question_id = uuid.uuid4().hex
                turn.status = TurnStatus.SUCCEEDED
                turn.question_id = question_id
                generation.status = GenerationStatus.WAITING_HUMAN
                generation.wait_reason = WaitReason.CLARIFICATION
                generation.outstanding_question_id = question_id
                generation.summary = result.final_message
                uow.insert_outbound_action(
                    OutboundAction(
                        issue_session_id=session.id,
                        generation_id=generation.id,
                        turn_id=turn.id,
                        kind="issue_note",
                        effect_key=f"note:{session.id}:{question_id}",
                        payload={
                            "template": "clarification",
                            "issue_iid": session.issue_iid,
                            "question": result.final_message,
                            "question_id": question_id,
                        },
                    )
                )
            elif outcome == TurnOutcomeKind.NO_SAFE_CHANGE:
                turn.status = TurnStatus.SUCCEEDED
                generation.status = GenerationStatus.WAITING_HUMAN
                generation.wait_reason = WaitReason.NO_SAFE_CHANGE
                generation.summary = result.final_message
                uow.insert_outbound_action(
                    OutboundAction(
                        issue_session_id=session.id,
                        generation_id=generation.id,
                        turn_id=turn.id,
                        kind="issue_note",
                        effect_key=f"note:{session.id}:{turn.id}:no_change",
                        payload={
                            "template": "no_safe_change",
                            "issue_iid": session.issue_iid,
                            "summary": result.final_message,
                        },
                    )
                )
            elif outcome == TurnOutcomeKind.BUDGET_EXHAUSTED:
                turn.status = TurnStatus.PAUSED_BUDGET
                generation.status = GenerationStatus.WAITING_HUMAN
                generation.wait_reason = WaitReason.BUDGET_EXHAUSTED
                generation.summary = result.final_message
            elif outcome == TurnOutcomeKind.FAILED or result.status == STATUS_FAILED:
                turn.status = TurnStatus.FAILED
                generation.status = GenerationStatus.FAILED
                generation.summary = result.final_message
                uow.insert_outbound_action(
                    OutboundAction(
                        issue_session_id=session.id,
                        generation_id=generation.id,
                        turn_id=turn.id,
                        kind="issue_note",
                        effect_key=f"note:{session.id}:{turn.id}:failed",
                        payload={
                            "template": "failed",
                            "issue_iid": session.issue_iid,
                            "summary": result.final_message,
                        },
                    )
                )
            else:
                turn.status = TurnStatus.SUCCEEDED
                generation.status = GenerationStatus.PUBLISHING
                generation.summary = result.final_message
                uow.insert_outbound_action(
                    OutboundAction(
                        issue_session_id=session.id,
                        generation_id=generation.id,
                        turn_id=turn.id,
                        kind="publish_changes",
                        effect_key=f"publish:{session.id}:{generation.id}:{turn.id}",
                        payload={
                            "summary": result.final_message,
                            "workspace_path": turn.workspace_path,
                            "issue_title": session.issue_title,
                            "issue_iid": session.issue_iid,
                        },
                    )
                )

            uow.save_turn(turn)
            uow.save_generation(generation)
            session.status = generation.status
            uow.save_session(session)
            uow.append_event(
                issue_session_id=session.id,
                generation_id=generation.id,
                turn_id=turn.id,
                kind="turn_outcome",
                payload={
                    "outcome": outcome.value,
                    "status": result.status,
                    "summary": result.final_message[:2000],
                    "steps": result.steps,
                },
            )


def _outcome_kind(result: AgentResult) -> TurnOutcomeKind:
    raw = result.outcome_kind
    if raw:
        try:
            return TurnOutcomeKind(raw)
        except ValueError:
            pass
    if result.status == STATUS_PAUSED:
        return TurnOutcomeKind.BUDGET_EXHAUSTED
    if result.status == STATUS_FAILED:
        return TurnOutcomeKind.FAILED
    return TurnOutcomeKind.CHANGES_READY
