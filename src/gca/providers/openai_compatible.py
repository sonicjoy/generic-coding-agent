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

from gca.providers.base import (
    LLMProvider,
    LLMResponse,
    Message,
    ProviderError,
    ToolCall,
    ToolSpec,
)
from gca.providers.tool_arguments import parse_tool_arguments
from gca.usage import LLMUsage

_MAX_RESPONSE_BYTES = 10_000_000


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _open_url(request: urllib.request.Request, timeout: int) -> Any:
    """Open a provider request without forwarding credentials through redirects."""

    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


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
        response_headers: dict[str, str] = {}
        try:
            with _open_url(request, self.timeout) as response:
                response_headers = {
                    str(key).lower(): str(value) for key, value in response.headers.items()
                }
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    raise ProviderError("LLM response exceeded 10 MB")
                data = json.loads(raw.decode())
        except urllib.error.HTTPError as exc:
            body = exc.read(20_001).decode(errors="replace")[:20_000]
            body = body.replace(api_key, "[REDACTED]")
            retryable = exc.code in {408, 409, 425, 429} or exc.code >= 500
            raise ProviderError(
                f"LLM HTTP {exc.code} for {self.model_id}: {body}",
                retryable=retryable,
            ) from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            raise ProviderError(
                f"LLM transport error for {self.model_id}: {exc}",
                retryable=True,
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError(f"invalid LLM response for {self.model_id}: {exc}") from exc

        try:
            choice = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"LLM response for {self.model_id} has no message choice") from exc
        content = _normalize_content(choice.get("content"))
        tool_calls: list[ToolCall] = []
        for raw in choice.get("tool_calls") or []:
            function = raw.get("function") or {}
            tool_calls.append(
                ToolCall(
                    id=str(raw.get("id") or f"call_{uuid.uuid4().hex[:8]}"),
                    name=str(function.get("name") or ""),
                    arguments=parse_tool_arguments(function.get("arguments") or {}),
                )
            )
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=_usage_from_response(data, response_headers, model_id=self.model_id),
        )


def _usage_from_response(
    data: dict[str, Any],
    headers: dict[str, str],
    *,
    model_id: str,
) -> LLMUsage | None:
    usage_raw = data.get("usage")
    cost_header = headers.get("x-openrouter-cost")
    generation_id = (
        headers.get("x-openrouter-generation-id") or headers.get("x-openrouter-id") or ""
    )
    if not isinstance(usage_raw, dict) and not cost_header and not generation_id:
        return None
    usage_raw = usage_raw if isinstance(usage_raw, dict) else {}
    prompt = int(usage_raw.get("prompt_tokens") or 0)
    completion = int(usage_raw.get("completion_tokens") or 0)
    total = int(usage_raw.get("total_tokens") or (prompt + completion))
    cost: float | None = None
    if cost_header:
        try:
            cost = float(cost_header)
        except ValueError:
            cost = None
    if prompt == 0 and completion == 0 and total == 0 and cost is None and not generation_id:
        return None
    return LLMUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cost_usd=cost,
        model=model_id,
        generation_id=str(generation_id),
    )


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
