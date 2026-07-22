"""Built-in OpenAI-compatible chat-completions provider.

Covers OpenRouter, OpenAI, Groq, Ollama, vLLM, and other hosts that speak the
OpenAI chat-completions + tool-calling protocol. API keys are read from
environment variables; never from config files.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from typing import Any
from urllib.parse import urljoin

from gca.providers.base import LLMProvider, LLMResponse, Message, ToolCall, ToolSpec


class OpenAICompatibleProvider(LLMProvider):
    """HTTP provider for OpenAI-compatible ``/chat/completions`` endpoints."""

    def __init__(
        self,
        *,
        model_id: str,
        base_url: str,
        api_key_env: str,
        timeout: int = 180,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        if not model_id.strip():
            raise ValueError("model_id must not be empty")
        if not base_url.strip():
            raise ValueError("base_url must not be empty")
        if not api_key_env.strip():
            raise ValueError("api_key_env must not be empty")
        self.model_id = model_id
        self.base_url = base_url.rstrip("/") + "/"
        self.api_key_env = api_key_env
        self.timeout = timeout
        self.default_headers = dict(default_headers or {})

    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        api_key = os.environ.get(self.api_key_env, "")
        if not api_key:
            raise RuntimeError(
                f"environment variable {self.api_key_env} is required for model {self.model_id}"
            )

        payload: dict[str, Any] = {
            "model": self.model_id,
            "messages": [_to_openai_message(message) for message in messages],
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters or {"type": "object", "properties": {}},
                    },
                }
                for tool in tools
            ]
            payload["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            **self.default_headers,
        }
        request = urllib.request.Request(
            urljoin(self.base_url, "chat/completions"),
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise RuntimeError(f"LLM HTTP {exc.code} for {self.model_id}: {body}") from exc

        choice = data["choices"][0]["message"]
        content = _normalize_content(choice.get("content"))
        tool_calls: list[ToolCall] = []
        for raw in choice.get("tool_calls") or []:
            function = raw.get("function") or {}
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments) if arguments.strip() else {}
                except json.JSONDecodeError:
                    arguments = {"_raw": arguments}
            if not isinstance(arguments, dict):
                arguments = {"_raw": arguments}
            tool_calls.append(
                ToolCall(
                    id=str(raw.get("id") or f"call_{uuid.uuid4().hex[:8]}"),
                    name=str(function.get("name") or ""),
                    arguments=dict(arguments),
                )
            )
        return LLMResponse(content=content, tool_calls=tool_calls)


def _normalize_content(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in {None, "text", "output_text"}:
                parts.append(str(part.get("text", "")))
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return str(content)


def _to_openai_message(message: Message) -> dict[str, Any]:
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id or "",
            "content": message.content,
        }
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments),
                },
            }
            for call in message.tool_calls
        ]
        if not payload["content"]:
            payload["content"] = None
    return payload
