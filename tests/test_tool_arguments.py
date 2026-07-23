from __future__ import annotations

import json

import pytest

from gca.providers import openai_compatible
from gca.providers.base import Message
from gca.providers.openai_compatible import OpenAICompatibleProvider
from gca.providers.tool_arguments import parse_tool_arguments, repair_over_escaped_argument_strings


def test_parse_tool_arguments_repairs_over_escaped_quotes() -> None:
    # After one JSON decode, model over-escape leaves literal backslash-quotes.
    raw = {
        "path": "mod.py",
        "content": '\\"""doc"""\nVALUE = \\"x\\"\n',
    }
    parsed = parse_tool_arguments(raw)
    assert parsed["content"] == '"""doc"""\nVALUE = "x"\n'


def test_parse_tool_arguments_handles_double_encoded_json_string() -> None:
    inner = {"path": "a.py", "content": "VALUE = 1\n"}
    double = json.dumps(json.dumps(inner))
    assert parse_tool_arguments(double) == inner


def test_parse_tool_arguments_preserves_normal_python() -> None:
    content = '"""Module."""\nVALUE = "ok"\n'
    assert parse_tool_arguments({"path": "a.py", "content": content})["content"] == content


def test_repair_leaves_non_escaped_strings_alone() -> None:
    args = {"summary": 'say "hello"'}
    assert repair_over_escaped_argument_strings(args) == args


def test_openai_compatible_repairs_over_escaped_write_file_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simulate OpenRouter: outer JSON decode already applied; arguments is a
    # JSON *string* whose content field still contains over-escaped quotes.
    arguments = json.dumps(
        {
            "path": "broken.py",
            "content": '\\"""Shared service dependencies."""\n',
        }
    )
    payload = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {
                                "name": "write_file",
                                "arguments": arguments,
                            },
                        }
                    ],
                }
            }
        ]
    }

    class FakeResponse:
        def read(self, size: int = -1) -> bytes:
            return json.dumps(payload).encode()

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setenv("TEST_API_KEY", "secret")
    monkeypatch.setattr(openai_compatible, "_open_url", lambda *a, **k: FakeResponse())
    provider = OpenAICompatibleProvider(
        model_id="test-model",
        base_url="https://example.test/v1",
        api_key_env="TEST_API_KEY",
    )
    response = provider.complete([Message(role="user", content="hi")], [])
    assert response.tool_calls[0].arguments["content"] == ('"""Shared service dependencies."""\n')
