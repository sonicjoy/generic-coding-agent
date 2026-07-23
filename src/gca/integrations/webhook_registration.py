"""GitLab webhook registration configuration and verification helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from typing import Any


class RegistrationError(ValueError):
    """Raised when webhook registration configuration is invalid."""


@dataclass(frozen=True)
class WebhookRegistration:
    """Operator-configured binding for one GitLab project webhook."""

    id: str
    gitlab_instance: str
    project_id: int
    project_path: str
    hook_uuid: str
    signing_secret: str
    trigger_label: str = "gca-run"
    minimum_actor_access_level: int = 30
    allow_legacy_token: bool = False
    enabled_events: frozenset[str] = frozenset(
        {"Issue Hook", "Note Hook", "Merge Request Hook", "Pipeline Hook"}
    )
    actor_allowlist: frozenset[int] = frozenset()
    bot_user_id: int | None = None
    repository_url: str = ""

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> WebhookRegistration:
        """Validate and build one registration from operator configuration."""

        registration_id = str(data.get("id", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", registration_id):
            raise RegistrationError("registration id must be 8-64 URL-safe characters")
        instance = str(data.get("gitlab_instance", "")).rstrip("/")
        if not instance.startswith("https://") and not instance.startswith("http://localhost"):
            raise RegistrationError("gitlab_instance must be an HTTPS URL")
        project_id = data.get("project_id")
        if isinstance(project_id, bool) or not isinstance(project_id, int) or project_id <= 0:
            raise RegistrationError("project_id must be a positive integer")
        project_path = str(data.get("project_path", "")).strip()
        if not project_path or "/" not in project_path:
            raise RegistrationError("project_path must look like group/repository")
        hook_uuid = str(data.get("hook_uuid", "")).strip()
        if not hook_uuid:
            raise RegistrationError("hook_uuid is required")
        secret = str(data.get("signing_secret", ""))
        if len(secret) < 16:
            raise RegistrationError("signing_secret must be at least 16 characters")
        access = data.get("minimum_actor_access_level", 30)
        if isinstance(access, bool) or not isinstance(access, int) or access < 10:
            raise RegistrationError("minimum_actor_access_level is invalid")
        events = data.get(
            "enabled_events",
            ["Issue Hook", "Note Hook", "Merge Request Hook", "Pipeline Hook"],
        )
        if not isinstance(events, list) or not all(isinstance(item, str) for item in events):
            raise RegistrationError("enabled_events must be a list of strings")
        allowlist = data.get("actor_allowlist", [])
        if not isinstance(allowlist, list) or not all(isinstance(item, int) for item in allowlist):
            raise RegistrationError("actor_allowlist must be a list of integers")
        bot_user_id = data.get("bot_user_id")
        if bot_user_id is not None and (
            isinstance(bot_user_id, bool) or not isinstance(bot_user_id, int)
        ):
            raise RegistrationError("bot_user_id must be an integer")
        return cls(
            id=registration_id,
            gitlab_instance=instance,
            project_id=project_id,
            project_path=project_path,
            hook_uuid=hook_uuid,
            signing_secret=secret,
            trigger_label=str(data.get("trigger_label", "gca-run")).strip() or "gca-run",
            minimum_actor_access_level=access,
            allow_legacy_token=bool(data.get("allow_legacy_token", False)),
            enabled_events=frozenset(events),
            actor_allowlist=frozenset(allowlist),
            bot_user_id=bot_user_id,
            repository_url=str(data.get("repository_url", "")).strip(),
        )


def parse_registrations(raw: str) -> dict[str, WebhookRegistration]:
    """Parse ``GCA_GITLAB_WEBHOOK_REGISTRATIONS`` JSON configuration."""

    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RegistrationError(f"invalid webhook registrations JSON: {exc}") from exc
    if not isinstance(value, list):
        raise RegistrationError("webhook registrations must be a JSON array")
    result: dict[str, WebhookRegistration] = {}
    for item in value:
        if not isinstance(item, dict):
            raise RegistrationError("each webhook registration must be an object")
        registration = WebhookRegistration.from_mapping(item)
        if registration.id in result:
            raise RegistrationError(f"duplicate registration id: {registration.id}")
        result[registration.id] = registration
    return result


def verify_gitlab_signature(
    *,
    headers: dict[str, str],
    body: bytes,
    registration: WebhookRegistration,
    now: float | None = None,
    max_skew_seconds: int = 300,
) -> None:
    """Verify Standard Webhooks HMAC or transitional legacy token auth."""

    from gca.integrations.webhooks import WebhookVerificationError

    lowered = {key.lower(): value for key, value in headers.items()}
    webhook_id = lowered.get("webhook-id") or lowered.get("idempotency-key", "")
    timestamp = lowered.get("webhook-timestamp", "")
    signature_header = lowered.get("webhook-signature", "")
    hook_uuid = lowered.get("x-gitlab-webhook-uuid", "")
    if hook_uuid and hook_uuid != registration.hook_uuid:
        raise WebhookVerificationError("webhook UUID does not match registration")
    if signature_header:
        if not webhook_id or not timestamp:
            raise WebhookVerificationError("missing Standard Webhooks headers")
        try:
            issued_at = int(timestamp)
        except ValueError as exc:
            raise WebhookVerificationError("invalid webhook timestamp") from exc
        current = time.time() if now is None else now
        if abs(current - issued_at) > max_skew_seconds:
            raise WebhookVerificationError("stale webhook timestamp")
        expected = _sign(registration.signing_secret, webhook_id, timestamp, body)
        candidates = []
        for item in signature_header.split():
            if item.startswith("v1,"):
                candidates.append(item.split(",", 1)[1])
        if not candidates or not any(
            hmac.compare_digest(candidate, expected) for candidate in candidates
        ):
            raise WebhookVerificationError("invalid webhook signature")
        return
    if registration.allow_legacy_token:
        token = lowered.get("x-gitlab-token", "")
        if token and hmac.compare_digest(token, registration.signing_secret):
            return
        raise WebhookVerificationError("invalid GitLab webhook token")
    raise WebhookVerificationError("webhook signature is required")


def _sign(secret: str, webhook_id: str, timestamp: str, body: bytes) -> str:
    message = f"{webhook_id}.{timestamp}.".encode() + body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()
