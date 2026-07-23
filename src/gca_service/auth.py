"""Authentication helpers for service-owned API routes."""

from __future__ import annotations

import hmac

from starlette.requests import Request


def is_authorized(request: Request, expected_token: str) -> bool:
    """Validate a bearer token with constant-time comparison."""

    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    return (
        separator == " "
        and scheme.lower() == "bearer"
        and bool(token)
        and hmac.compare_digest(token, expected_token)
    )
