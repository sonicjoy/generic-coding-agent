from __future__ import annotations

from pathlib import Path

from gca.agent import Agent, AgentConfig
from gca.credentials import CredentialBroker
from gca.providers.scripted import ScriptedProvider
from gca.session import SessionStore
from gca.tools import build_registry
from gca.tools.base import Tool, ToolContext, ToolResult


class SecretEchoTool(Tool):
    name = "secret_echo"
    description = "Return an authorized secret for redaction testing."
    parameters = {"type": "object", "properties": {}}

    def run(self, ctx: ToolContext, **kwargs: object) -> ToolResult:
        return ToolResult.success(f"value={ctx.secret('SERVICE_TOKEN')}")


def test_tool_secret_is_redacted_before_session_persistence(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    session = store.create("Exercise secret redaction")
    provider = ScriptedProvider.from_script(
        [
            {"tool_calls": [{"name": "secret_echo", "arguments": {}}]},
            {
                "tool_calls": [
                    {
                        "name": "finish",
                        "arguments": {"summary": "Secret output was handled."},
                    }
                ]
            },
        ]
    )
    registry = build_registry()
    registry.register(SecretEchoTool())
    context = ToolContext(
        workspace=tmp_path,
        credentials=CredentialBroker({"SERVICE_TOKEN": "super-secret-value"}),
        tool_secret_access={"secret_echo": frozenset({"SERVICE_TOKEN"})},
    )

    result = Agent(
        provider=provider,
        registry=registry,
        session=session,
        context=context,
        store=store,
        config=AgentConfig(max_steps=5),
    ).run()

    assert result.status == "completed"
    persisted = (tmp_path / "sessions" / f"{session.id}.json").read_text(encoding="utf-8")
    assert "super-secret-value" not in persisted
    assert "[REDACTED]" in persisted
