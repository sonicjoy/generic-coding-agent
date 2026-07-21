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

from gca.providers.base import LLMProvider, Message
from gca.session import STATUS_COMPLETED, STATUS_FAILED, STATUS_PAUSED, Session, SessionStore
from gca.tools.base import ToolContext, ToolError, ToolRegistry
from gca.tools.control import FINISH_TOOL_NAME

# Callback invoked with human-readable progress events (e.g. for CLI logging).
EventHook = Callable[[str], None]


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
        store: SessionStore | None = None,
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

    def _emit(self, message: str) -> None:
        if self._on_event is not None:
            self._on_event(message)

    def _persist(self) -> None:
        if self.store is not None:
            self.store.save(self.session)

    def run(self) -> AgentResult:
        session = self.session
        final_message = ""

        while session.step_count < self.config.max_steps:
            response = self.provider.complete(session.messages, self.registry.specs())
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
                final_message = response.content
                session.status = STATUS_COMPLETED
                self._persist()
                break

            finished = False
            for call in response.tool_calls:
                output, ok = self._run_tool(call.name, call.arguments)
                self._emit(f"[tool] {call.name} -> {'ok' if ok else 'error'}")
                session.messages.append(Message(role="tool", content=output, tool_call_id=call.id))
                if call.name == FINISH_TOOL_NAME:
                    finished = True
                    final_message = output

            self._persist()
            if finished:
                session.status = STATUS_COMPLETED
                self._persist()
                break
        else:
            session.status = STATUS_PAUSED
            final_message = f"Step budget ({self.config.max_steps}) exhausted."
            self._persist()

        return AgentResult(
            status=session.status,
            steps=session.step_count,
            final_message=final_message,
        )

    def _run_tool(self, name: str, arguments: dict[str, object]) -> tuple[str, bool]:
        tool = self.registry.get(name)
        if tool is None:
            return (f"error: unknown tool '{name}'", False)
        try:
            result = tool.run(self.context, **arguments)
        except ToolError as exc:
            return (f"error: {exc}", False)
        except Exception as exc:  # defensive: never let a tool crash the loop
            self.session.status = STATUS_FAILED
            return (f"error: tool '{name}' raised {type(exc).__name__}: {exc}", False)
        return (result.output, result.ok)
