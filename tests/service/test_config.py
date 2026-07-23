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
            "GCA_DEFAULT_MAX_STEPS": "100",
            "GCA_TOOL_SECRET_GRANTS": (
                '{"github.com/owner/repo":{"query_metrics":["METRICS_TOKEN"]}}'
            ),
        }
    )

    assert settings.allowed_repository_hosts == frozenset({"github.com", "gitlab.example"})
    assert settings.default_max_steps == 100
    assert settings.tool_secret_grants["github.com/owner/repo"]["query_metrics"] == frozenset(
        {"METRICS_TOKEN"}
    )
    assert "api-token-123456" not in repr(settings)
    assert "scm-secret" not in repr(settings)


def test_default_max_steps_is_optional_and_bounded() -> None:
    unset = ServiceSettings.from_environment(
        {
            "GCA_API_TOKEN": "api-token-123456",
            "GCA_ALLOWED_REPOSITORY_HOSTS": "github.com",
        }
    )
    assert unset.default_max_steps is None
    with pytest.raises(ServiceConfigError, match="GCA_DEFAULT_MAX_STEPS"):
        ServiceSettings.from_environment(
            {
                "GCA_API_TOKEN": "api-token-123456",
                "GCA_ALLOWED_REPOSITORY_HOSTS": "github.com",
                "GCA_DEFAULT_MAX_STEPS": "0",
            }
        )
    with pytest.raises(ServiceConfigError, match="GCA_DEFAULT_MAX_STEPS"):
        ServiceSettings.from_environment(
            {
                "GCA_API_TOKEN": "api-token-123456",
                "GCA_ALLOWED_REPOSITORY_HOSTS": "github.com",
                "GCA_DEFAULT_MAX_STEPS": "nope",
            }
        )


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


def test_service_owned_tokens_cannot_be_granted_to_repository_tools() -> None:
    with pytest.raises(ServiceConfigError, match="service-owned secrets"):
        ServiceSettings.from_environment(
            {
                "GCA_API_TOKEN": "api-token-123456",
                "GCA_ALLOWED_REPOSITORY_HOSTS": "github.com",
                "GCA_TOOL_SECRET_GRANTS": (
                    '{"github.com/owner/repo":{"run_tests":["GCA_GITHUB_TOKEN"]}}'
                ),
            }
        )


def test_tool_secret_grants_reject_wildcard_tools() -> None:
    with pytest.raises(ServiceConfigError, match="invalid tool secret grant"):
        ServiceSettings.from_environment(
            {
                "GCA_API_TOKEN": "api-token-123456",
                "GCA_ALLOWED_REPOSITORY_HOSTS": "github.com",
                "GCA_TOOL_SECRET_GRANTS": ('{"github.com/owner/repo":{"*":["METRICS_TOKEN"]}}'),
            }
        )
