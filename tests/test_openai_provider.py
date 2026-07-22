from __future__ import annotations

import io
import urllib.error

import pytest

from gca.providers import openai_compatible
from gca.providers.base import ProviderError
from gca.providers.openai_compatible import OpenAICompatibleProvider


@pytest.mark.parametrize(("status", "retryable"), [(429, True), (503, True), (400, False)])
def test_http_provider_classifies_retryable_statuses(
    monkeypatch: object,
    status: int,
    retryable: bool,
) -> None:
    monkeypatch.setenv("TEST_LLM_KEY", "secret")  # type: ignore[attr-defined]

    def fail(*args: object, **kwargs: object) -> object:
        raise urllib.error.HTTPError(
            "https://example.test",
            status,
            "failure",
            {},
            io.BytesIO(b"provider error"),
        )

    monkeypatch.setattr(openai_compatible, "_open_url", fail)  # type: ignore[attr-defined]
    provider = OpenAICompatibleProvider(
        model_id="model",
        base_url="https://example.test/v1",
        api_key_env="TEST_LLM_KEY",
    )

    with pytest.raises(ProviderError) as captured:
        provider.complete([], [])

    assert captured.value.retryable is retryable
