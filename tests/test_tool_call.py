from __future__ import annotations

from gca.providers.base import ToolCall


def test_tool_call_from_dict_parses_json_string_arguments() -> None:
    call = ToolCall.from_dict(
        {
            "id": "call_1",
            "name": "write_file",
            "arguments": '{"path": "a.py", "content": "VALUE = 1\\n"}',
        }
    )
    assert call.arguments == {"path": "a.py", "content": "VALUE = 1\n"}


def test_tool_call_from_dict_keeps_dict_arguments() -> None:
    call = ToolCall.from_dict(
        {
            "id": "call_2",
            "name": "read_file",
            "arguments": {"path": "a.py"},
        }
    )
    assert call.arguments == {"path": "a.py"}


def test_tool_call_from_dict_preserves_invalid_json_as_raw() -> None:
    call = ToolCall.from_dict(
        {
            "id": "call_3",
            "name": "write_file",
            "arguments": "{not-json",
        }
    )
    assert call.arguments == {"_raw": "{not-json"}
