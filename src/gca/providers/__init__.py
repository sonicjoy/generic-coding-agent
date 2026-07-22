"""LLM provider abstraction and built-in providers.

The harness is provider-agnostic: it depends only on the :class:`LLMProvider`
interface defined in :mod:`gca.providers.base`. Built-in options include an
OpenAI-compatible HTTP client (for OpenRouter, OpenAI, Groq, Ollama, etc.) and a
deterministic :class:`ScriptedProvider` for offline demos/tests. Custom backends
can still be registered via plugins.
"""

from gca.providers.base import LLMProvider, LLMResponse, Message, ToolCall, ToolSpec
from gca.providers.openai_compatible import OpenAICompatibleProvider
from gca.providers.scripted import ScriptedProvider

__all__ = [
    "LLMProvider",
    "LLMResponse",
    "Message",
    "ToolCall",
    "ToolSpec",
    "OpenAICompatibleProvider",
    "ScriptedProvider",
]
