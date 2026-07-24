"""Normalize LLM tool-call argument payloads.

OpenAI-compatible hosts (including OpenRouter) return function.arguments as a
JSON object or a JSON-encoded string. Some model responses over-escape quotes
inside string fields, which then land on disk as literal backslashes. This
module parses and lightly repairs those payloads before tools run.
"""

from __future__ import annotations

import json
from typing import Any

from gca.text_escape import looks_json_over_escaped


def parse_tool_arguments(value: Any) -> dict[str, Any]:
    """Parse provider/session tool arguments into a plain dict.

    Handles already-decoded objects, JSON strings, double-encoded JSON strings,
    and over-escaped quote sequences inside string fields.
    """

    arguments: Any = value
    for _ in range(2):
        if not isinstance(arguments, str):
            break
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            return {"_raw": arguments}
    if not isinstance(arguments, dict):
        return {"_raw": arguments}
    return repair_over_escaped_argument_strings(dict(arguments))


def repair_over_escaped_argument_strings(arguments: dict[str, Any]) -> dict[str, Any]:
    """Undo JSON-style quote over-escaping in string argument values."""

    repaired: dict[str, Any] = {}
    for key, value in arguments.items():
        if isinstance(value, str) and looks_json_over_escaped(value):
            repaired[key] = _unescape_json_quotes(value)
        else:
            repaired[key] = value
    return repaired


def _unescape_json_quotes(value: str) -> str:
    """Convert literal backslash-quote sequences into quotes when over-escaped."""

    # Prefer a full JSON-string decode when the value has no raw newlines/breaks.
    if "\n" not in value and "\r" not in value:
        try:
            decoded = json.loads(f'"{value}"')
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, str) and decoded != value:
            return decoded
    # Multi-line tool payloads (write_file / apply_patch) commonly keep real
    # newlines while still over-escaping quotes — strip that layer directly.
    return value.replace('\\"', '"')
