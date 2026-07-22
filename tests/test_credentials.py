from __future__ import annotations

from gca.credentials import CredentialBroker


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
