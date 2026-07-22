"""Core LLM provider interface and message/tool data structures.

These types form the stable contract between the harness and any LLM backend.
A provider only needs to translate the harness :class:`Message`/:class:`ToolSpec`
inputs into its own API calls and translate the response back into an
:class:`LLMResponse`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    """A request from the model to invoke a tool with JSON arguments."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolCall:
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            arguments=dict(data.get("arguments", {})),
        )


@dataclass
class Message:
    """A single conversation message.

    ``role`` is one of ``"system"``, ``"user"``, ``"assistant"`` or ``"tool"``.
    Assistant messages may carry ``tool_calls``; tool result messages carry a
    ``tool_call_id`` referencing the call they answer.
    """

    role: str
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "tool_calls": [tc.to_dict() for tc in self.tool_calls],
            "tool_call_id": self.tool_call_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        return cls(
            role=str(data["role"]),
            content=str(data.get("content", "")),
            tool_calls=[ToolCall.from_dict(tc) for tc in data.get("tool_calls", [])],
            tool_call_id=data.get("tool_call_id"),
        )


@dataclass
class ToolSpec:
    """A tool advertised to the model, described with a JSON-schema parameter block."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    """The model's reply: free-form text and/or a set of tool calls."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMProvider(ABC):
    """Provider-agnostic completion interface.

    Implementations receive the running conversation plus the available tool
    specs and must return an :class:`LLMResponse`. Whether the backend supports
    native tool/function calling or requires prompt-based emulation is entirely
    the provider's concern.
    """

    @abstractmethod
    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        """Produce the next assistant response given the conversation and tools."""
        raise NotImplementedError

    def get_state(self) -> dict[str, Any]:
        """Return JSON-serializable provider state needed to resume, if any."""

        return {}

    def set_state(self, state: dict[str, Any]) -> None:
        """Restore state returned by :meth:`get_state`.

        Network-backed providers are normally stateless and can use this default
        implementation. Deterministic or local providers may override it.
        """
