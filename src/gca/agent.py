"""The core agentic loop.

The :class:`Agent` drives the observe -> reason -> act cycle:

1. Ask the provider for the next step given the conversation and available tools.
2. If the model returned tool calls, execute each one and feed results back.
3. Repeat until the model calls ``finish``, returns no tool calls, or the step
   budget is exhausted.

All state lives in a :class:`~gca.session.Session`, which is persisted after each
step so a run can be resumed. The loop is provider-agnostic and tool-agnostic:
capabilities come entirely from the injected registry.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from gca.providers.base import LLMProvider, Message, ToolCall
from gca.session import STATUS_ACTIVE, STATUS_COMPLETED, STATUS_FAILED, STATUS_PAUSED, Session
from gca.tools.base import ToolContext, ToolError, ToolRegistry
from gca.tools.control import FINISH_TOOL_NAME

# Callback invoked with human-readable progress events (e.g. for CLI logging).
EventHook = Callable[[str], None]


class SessionSaver(Protocol):
    """Structural contract for persisting agent session progress."""

    def save(self, session: Session) -> None:
        """Persist ``session``."""


@dataclass
class AgentConfig:
    max_steps: int = 25


@dataclass
class AgentResult:
    status: str
    steps: int
    final_message: str


class Agent:
    """Runs a task to completion using a provider, tools, and a session."""

    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        session: Session,
        context: ToolContext,
        store: SessionSaver | None = None,
        config: AgentConfig | None = None,
        on_event: EventHook | None = None,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.session = session
        self.context = context
        self.store = store
        self.config = config or AgentConfig()
        self._on_event = on_event
        if session.provider_state:
            provider.set_state(dict(session.provider_state))

    def _emit(self, message: str) -> None:
        if self._on_event is not None:
            self._on_event(message)

    def _persist(self) -> None:
        self.session.provider_state = self.provider.get_state()
        if self.store is not None:
            self.store.save(self.session)

    def run(self) -> AgentResult:
        session = self.session
        if session.status in {STATUS_COMPLETED, STATUS_FAILED}:
            return self._result()
        if session.inflight_tool_call_id:
            session.status = STATUS_FAILED
            session.final_message = (
                f"Tool call '{session.inflight_tool_call_id}' was interrupted; "
                "its side effects are unknown."
            )
            self._persist()
            return self._result()
        session.status = STATUS_ACTIVE

        while True:
            pending_calls = self._pending_tool_calls()
            if pending_calls:
                if self._run_tool_calls(pending_calls):
                    return self._result()
                continue
            if session.step_count >= self.config.max_steps:
                session.status = STATUS_PAUSED
                session.final_message = f"Step budget ({self.config.max_steps}) exhausted."
                self._persist()
                return self._result()

            response = self.provider.complete(session.messages, self.registry.specs())
            response.content = self.context.redact(response.content)
            session.step_count += 1
            session.messages.append(
                Message(
                    role="assistant",
                    content=response.content,
                    tool_calls=response.tool_calls,
                )
            )
            if response.content:
                self._emit(f"[assistant] {response.content}")

            if not response.tool_calls:
                session.status = STATUS_COMPLETED
                session.final_message = response.content
                self._persist()
                return self._result()

            # Persist the provider cursor and assistant response before executing
            # side effects. Unanswered calls can then be resumed deterministically.
            self._persist()

    def _result(self) -> AgentResult:
        return AgentResult(
            status=self.session.status,
            steps=self.session.step_count,
            final_message=self.session.final_message,
        )

    def _pending_tool_calls(self) -> list[ToolCall]:
        answered = {
            message.tool_call_id
            for message in self.session.messages
            if message.role == "tool" and message.tool_call_id is not None
        }
        return [
            call
            for message in self.session.messages
            if message.role == "assistant"
            for call in message.tool_calls
            if call.id not in answered
        ]

    def _run_tool_calls(self, calls: list[ToolCall]) -> bool:
        for call in calls:
            self.session.inflight_tool_call_id = call.id
            self._persist()
            output, ok, fatal = self._run_tool(call.name, call.arguments)
            self._emit(f"[tool] {call.name} -> {'ok' if ok else 'error'}")
            self.session.messages.append(Message(role="tool", content=output, tool_call_id=call.id))
            self.session.inflight_tool_call_id = ""
            if fatal:
                self.session.status = STATUS_FAILED
                self.session.final_message = output
                self._persist()
                return True
            if call.name == FINISH_TOOL_NAME and ok:
                self.session.status = STATUS_COMPLETED
                self.session.final_message = output
                self._persist()
                return True
            self._persist()
        return False

    def _run_tool(self, name: str, arguments: dict[str, object]) -> tuple[str, bool, bool]:
        if not self.context.allows(name):
            return (
                f"error: tool '{name}' is not allowed in phase {self.context.phase}",
                False,
                False,
            )
        tool = self.registry.get(name)
        if tool is None:
            return (f"error: unknown tool '{name}'", False, False)
        try:
            result = tool.run(self.context.for_tool(name), **arguments)
        except ToolError as exc:
            return (self.context.redact(f"error: {exc}"), False, False)
        except Exception as exc:  # defensive: never let a tool crash the loop
            return (
                self.context.redact(f"error: tool '{name}' raised {type(exc).__name__}: {exc}"),
                False,
                True,
            )
        return (self.context.redact(result.output), result.ok, False)
