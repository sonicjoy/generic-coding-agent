from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gca.model_config import (
    ModelConfigError,
    build_registry_from_catalog,
    load_dotenv,
    load_model_catalog,
)
from gca.providers.base import Message
from gca.providers.openai_compatible import OpenAICompatibleProvider


def test_loads_and_builds_models_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    catalog_path = tmp_path / "models.yaml"
    catalog_path.write_text(
        """
providers:
  openrouter:
    type: openai_compatible
    base_url: https://openrouter.ai/api/v1
    api_key_env: OPENROUTER_API_KEY
models:
  luna:
    provider: openrouter
    model_id: openai/gpt-5.6-luna
    strength: 3
    speed: 5
    cost: 1
  opus:
    provider: openrouter
    model_id: anthropic/claude-opus-4.8
    strength: 5
    speed: 2
    cost: 5
""",
        encoding="utf-8",
    )

    catalog = load_model_catalog([catalog_path])
    registry = build_registry_from_catalog(catalog)

    assert registry.names() == ["luna", "opus"]
    assert registry.select(capability="planning", strategy="strongest").name == "opus"
    assert registry.select(capability="coding", strategy="efficient").name == "luna"

    provider = registry.get("luna")
    assert provider is not None
    assert isinstance(provider.provider, OpenAICompatibleProvider)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        provider.provider.complete([Message(role="user", content="hi")], [])


def test_catalog_merge_later_wins(tmp_path: Path) -> None:
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    first.write_text(
        """
providers:
  openrouter:
    type: openai_compatible
    base_url: https://openrouter.ai/api/v1
    api_key_env: OPENROUTER_API_KEY
models:
  fast:
    provider: openrouter
    model_id: x-ai/grok-4.5
    cost: 2
""",
        encoding="utf-8",
    )
    second.write_text(
        """
models:
  fast:
    provider: openrouter
    model_id: openai/gpt-5.6-luna
    cost: 1
""",
        encoding="utf-8",
    )

    catalog = load_model_catalog([first, second])
    assert catalog.models["fast"].model_id == "openai/gpt-5.6-luna"
    assert catalog.models["fast"].cost == 1


def test_rejects_unknown_provider_reference(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(
        """
models:
  broken:
    provider: missing
    model_id: x
""",
        encoding="utf-8",
    )
    catalog = load_model_catalog([path])
    with pytest.raises(ModelConfigError, match="unknown provider"):
        build_registry_from_catalog(catalog)


def test_load_dotenv_does_not_override_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("DEMO_KEY=from-file\nKEEP=file\n", encoding="utf-8")
    monkeypatch.setenv("KEEP", "existing")
    monkeypatch.delenv("DEMO_KEY", raising=False)

    load_dotenv(env_path)

    assert os.environ["DEMO_KEY"] == "from-file"
    assert os.environ["KEEP"] == "existing"


def test_openai_compatible_parses_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": "Working",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {
                                "name": "finish",
                                "arguments": '{"summary":"done"}',
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

    def fake_urlopen(request: object, timeout: int = 0) -> FakeResponse:
        return FakeResponse()

    monkeypatch.setenv("TEST_API_KEY", "secret")
    monkeypatch.setattr("gca.providers.openai_compatible._open_url", fake_urlopen)

    provider = OpenAICompatibleProvider(
        model_id="test-model",
        base_url="https://example.test/v1",
        api_key_env="TEST_API_KEY",
    )
    response = provider.complete([Message(role="user", content="hi")], [])
    assert response.content == "Working"
    assert response.tool_calls[0].name == "finish"
    assert response.tool_calls[0].arguments == {"summary": "done"}


@pytest.mark.parametrize(
    ("provider_config", "message"),
    [
        (
            "base_url: https://user@example.test/v1\n    api_key_env: TEST_KEY",
            "must not contain credentials",
        ),
        (
            "base_url: https://example.test/v1?token=x\n    api_key_env: TEST_KEY",
            "must not contain credentials",
        ),
        (
            "base_url: https://example.test/v1\n    api_key_env: invalid-name",
            "valid environment name",
        ),
        (
            "base_url: https://example.test/v1\n"
            "    api_key_env: TEST_KEY\n"
            "    headers:\n"
            "      Authorization: committed-secret",
            "secret-bearing names",
        ),
    ],
)
def test_rejects_secret_bearing_provider_configuration(
    tmp_path: Path,
    provider_config: str,
    message: str,
) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(
        f"providers:\n  unsafe:\n    {provider_config}\nmodels: {{}}\n",
        encoding="utf-8",
    )

    with pytest.raises(ModelConfigError, match=message):
        load_model_catalog([path])
