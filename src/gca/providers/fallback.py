"""Runtime failover across an ordered chain of LLM providers."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from gca.providers.base import LLMProvider, LLMResponse, Message, ProviderError, ToolSpec

FailoverHook = Callable[[str, str, str], None]


class FallbackProvider(LLMProvider):
    """Try providers in order; on retryable failures, advance to the next.

    Used when routing configures a preference list such as
    ``[claude-fable-5, claude-opus-4.8]`` so an unresponsive primary can
    fail over without failing the whole run.
    """

    def __init__(
        self,
        chain: Sequence[tuple[str, LLMProvider]],
        *,
        on_failover: FailoverHook | None = None,
    ) -> None:
        if not chain:
            raise ValueError("fallback provider chain must not be empty")
        names = [name for name, _ in chain]
        if any(not name.strip() for name in names):
            raise ValueError("fallback provider names must be non-empty")
        if len(set(names)) != len(names):
            raise ValueError("fallback provider names must be unique")
        self._chain = list(chain)
        self._index = 0
        self._on_failover = on_failover

    @property
    def active_name(self) -> str:
        """Return the model name currently selected in the chain."""

        return self._chain[self._index][0]

    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        """Complete with the active provider, advancing on retryable errors."""

        while True:
            name, provider = self._chain[self._index]
            try:
                return provider.complete(messages, tools)
            except ProviderError as exc:
                if not exc.retryable or self._index >= len(self._chain) - 1:
                    raise
                nxt_name = self._chain[self._index + 1][0]
                if self._on_failover is not None:
                    self._on_failover(name, nxt_name, str(exc))
                self._index += 1

    def get_state(self) -> dict[str, Any]:
        """Persist active model plus the active provider's own state."""

        _, provider = self._chain[self._index]
        return {
            "fallback_index": self._index,
            "active_name": self.active_name,
            "provider_state": provider.get_state(),
        }

    def set_state(self, state: dict[str, Any]) -> None:
        """Restore failover position and the active provider's state.

        Prefers ``active_name`` so resume stays on the model that last succeeded
        even when the in-memory chain is rebuilt from the current binding.
        Bare provider states (without wrapper keys) are forwarded to the active
        provider for backward compatibility with pre-fallback sessions.
        """

        if not isinstance(state, dict):
            return
        wrapped = any(key in state for key in ("fallback_index", "active_name", "provider_state"))
        if not wrapped:
            self._chain[self._index][1].set_state(state)
            return

        active = state.get("active_name")
        if isinstance(active, str) and active:
            for index, (name, _) in enumerate(self._chain):
                if name == active:
                    self._index = index
                    break
        else:
            raw_index = state.get("fallback_index", 0)
            if isinstance(raw_index, bool) or not isinstance(raw_index, int):
                raw_index = 0
            self._index = max(0, min(raw_index, len(self._chain) - 1))

        _, provider = self._chain[self._index]
        provider_state = state.get("provider_state", {})
        if isinstance(provider_state, dict):
            provider.set_state(provider_state)
