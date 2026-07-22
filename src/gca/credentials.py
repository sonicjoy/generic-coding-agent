"""Credential isolation and output redaction for tools and subprocesses."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

_SENSITIVE_NAME = re.compile(
    r"(?:^|_)(?:api_?key|token|secret|password|passwd|credential|private_?key|auth)(?:$|_)",
    re.IGNORECASE,
)
_HOSTED_SAFE_ENV = frozenset(
    {
        "CI",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PYTHONPATH",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "VIRTUAL_ENV",
    }
)


def is_sensitive_name(name: str) -> bool:
    """Return whether an environment variable name likely contains a credential."""

    return bool(_SENSITIVE_NAME.search(name))


@dataclass
class CredentialBroker:
    """Hold secrets separately from child-process environments."""

    secrets: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> CredentialBroker:
        """Capture sensitive values for redaction and authorized tool lookup."""

        source = dict(environ or os.environ)
        return cls(
            secrets={
                name: value for name, value in source.items() if value and is_sensitive_name(name)
            }
        )

    def get(self, name: str, *, allowed: frozenset[str]) -> str:
        """Return a secret only when the tool context authorizes its name."""

        if name not in allowed:
            raise PermissionError(f"secret is not authorized for this tool: {name}")
        try:
            return self.secrets[name]
        except KeyError as exc:
            raise KeyError(f"required secret is not configured: {name}") from exc

    def subprocess_env(
        self,
        profile: str,
        *,
        environ: Mapping[str, str] | None = None,
        allowed_keys: frozenset[str] = frozenset(),
    ) -> dict[str, str]:
        """Build a child environment without inherited credentials."""

        source = dict(environ or os.environ)
        safe: dict[str, str] = {}
        for name, value in source.items():
            if is_sensitive_name(name):
                continue
            if profile == "hosted" and name not in _HOSTED_SAFE_ENV and name not in allowed_keys:
                continue
            safe[name] = value
        return safe

    def redact(self, text: str) -> str:
        """Replace captured secret values with a stable marker."""

        redacted = text
        for value in sorted(set(self.secrets.values()), key=len, reverse=True):
            if len(value) >= 4:
                redacted = redacted.replace(value, "[REDACTED]")
        return redacted
