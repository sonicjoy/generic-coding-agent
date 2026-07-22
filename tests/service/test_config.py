from __future__ import annotations

import pytest

from gca_service.config import ServiceConfigError, ServiceSettings


def test_environment_settings_are_fail_closed_and_hide_secrets() -> None:
    settings = ServiceSettings.from_environment(
        {
            "GCA_API_TOKEN": "api-token-123456",
            "GCA_DATA_DIR": "/tmp/gca-service-test",
            "GCA_ALLOWED_REPOSITORY_HOSTS": "github.com,gitlab.example",
            "GCA_GITHUB_TOKEN": "scm-secret",
        }
    )

    assert settings.allowed_repository_hosts == frozenset({"github.com", "gitlab.example"})
    assert "api-token-123456" not in repr(settings)
    assert "scm-secret" not in repr(settings)


def test_settings_require_auth_and_repository_allowlist() -> None:
    with pytest.raises(ServiceConfigError, match="GCA_API_TOKEN"):
        ServiceSettings.from_environment({})
    with pytest.raises(ServiceConfigError, match="at least 16"):
        ServiceSettings.from_environment(
            {
                "GCA_API_TOKEN": "short",
                "GCA_ALLOWED_REPOSITORY_HOSTS": "github.com",
            }
        )
    with pytest.raises(ServiceConfigError, match="allowed_repository_hosts"):
        ServiceSettings.from_environment({"GCA_API_TOKEN": "api-token-123456"})


def test_webhook_secret_requires_project_allowlist() -> None:
    with pytest.raises(ServiceConfigError, match="project allowlist"):
        ServiceSettings.from_environment(
            {
                "GCA_API_TOKEN": "api-token-123456",
                "GCA_ALLOWED_REPOSITORY_HOSTS": "github.com",
                "GCA_GITHUB_WEBHOOK_SECRET": "webhook-secret-123456",
            }
        )
