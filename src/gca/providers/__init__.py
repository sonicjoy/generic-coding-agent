"""LLM provider abstraction and built-in providers.

The harness is provider-agnostic: it depends only on the :class:`LLMProvider`
interface defined in :mod:`gca.providers.base`. Users wire in their own provider
(OpenAI, Anthropic, a local model, etc.) by implementing that interface and
registering it via a plugin. A deterministic :class:`ScriptedProvider` is shipped
for testing and demos so the harness can run end-to-end without any credentials.
"""

from gca.providers.base import LLMProvider, LLMResponse, Message, ToolCall, ToolSpec
from gca.providers.scripted import ScriptedProvider

__all__ = [
    "LLMProvider",
    "LLMResponse",
    "Message",
    "ToolCall",
    "ToolSpec",
    "ScriptedProvider",
]
