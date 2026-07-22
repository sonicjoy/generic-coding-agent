from __future__ import annotations

from pathlib import Path

import pytest

from gca.credentials import CredentialBroker
from gca.tools.base import ToolContext, ToolError


def test_broker_redacts_values_and_sanitizes_child_environment() -> None:
    source = {
        "PATH": "/bin",
        "OPENROUTER_API_KEY": "secret-value-123",
        "NORMAL_SETTING": "visible",
    }
    broker = CredentialBroker.from_environment(source)

    local = broker.subprocess_env("local", environ=source)
    hosted = broker.subprocess_env("hosted", environ=source)

    assert "OPENROUTER_API_KEY" not in local
    assert local["NORMAL_SETTING"] == "visible"
    assert hosted == {"PATH": "/bin"}
    assert broker.redact("token=secret-value-123") == "token=[REDACTED]"


def test_tool_context_scopes_secret_access_by_tool(tmp_path: Path) -> None:
    broker = CredentialBroker({"SERVICE_TOKEN": "secret-value"})
    context = ToolContext(
        workspace=tmp_path,
        credentials=broker,
        tool_secret_access={"approved": frozenset({"SERVICE_TOKEN"})},
    )

    assert context.for_tool("approved").secret("SERVICE_TOKEN") == "secret-value"
    with pytest.raises(ToolError, match="not authorized"):
        context.for_tool("other").secret("SERVICE_TOKEN")
