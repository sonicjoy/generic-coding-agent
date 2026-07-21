"""A deterministic provider used for tests and demos.

The :class:`ScriptedProvider` replays a predetermined list of
:class:`LLMResponse` steps, one per ``complete`` call. This lets the harness be
exercised end-to-end (loop, tools, patching, sessions) without any network access
or credentials, giving reproducible behaviour in CI.
"""

from __future__ import annotations

from typing import Any

from gca.providers.base import LLMProvider, LLMResponse, Message, ToolCall, ToolSpec


class ScriptedProvider(LLMProvider):
    """Replays a fixed sequence of responses.

    Each element of ``steps`` is consumed on successive ``complete`` calls. Once
    the script is exhausted the provider returns a final, tool-call-free message,
    which the harness treats as a natural stop.
    """

    def __init__(self, steps: list[LLMResponse], final_text: str = "Done.") -> None:
        self._steps = list(steps)
        self._index = 0
        self._final_text = final_text

    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        if self._index >= len(self._steps):
            return LLMResponse(content=self._final_text)
        step = self._steps[self._index]
        self._index += 1
        return step

    @classmethod
    def from_script(
        cls, script: list[dict[str, Any]], final_text: str = "Done."
    ) -> ScriptedProvider:
        """Build a provider from a plain list of dicts (e.g. parsed from JSON).

        Each entry may contain ``content`` and/or ``tool_calls`` where a tool call
        is ``{"name": ..., "arguments": {...}}``.
        """

        steps: list[LLMResponse] = []
        for i, entry in enumerate(script):
            tool_calls = [
                ToolCall(
                    id=str(tc.get("id", f"call_{i}_{j}")),
                    name=str(tc["name"]),
                    arguments=dict(tc.get("arguments", {})),
                )
                for j, tc in enumerate(entry.get("tool_calls", []))
            ]
            steps.append(LLMResponse(content=str(entry.get("content", "")), tool_calls=tool_calls))
        return cls(steps, final_text=final_text)
