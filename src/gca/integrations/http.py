"""Small JSON HTTP client shared by optional SCM adapters."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class IntegrationHttpError(RuntimeError):
    """Raised when an integration API request fails."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> Any:
    """Issue one bounded JSON request."""

    payload = request_bytes(
        method,
        url,
        headers={**headers, "Accept": "application/json", "Content-Type": "application/json"},
        body=None if body is None else json.dumps(body).encode("utf-8"),
        timeout=timeout,
        max_bytes=2_000_000,
    )
    return json.loads(payload.decode("utf-8")) if payload else None


def request_bytes(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: bytes | None = None,
    timeout: int = 30,
    max_bytes: int = 2_000_000,
) -> bytes:
    """Issue one bounded raw HTTP request without following redirects."""

    parsed = urlparse(url)
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise IntegrationHttpError("integration API URLs must use HTTPS")
    request = Request(
        url,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        opener = build_opener(_NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            payload = response.read(max_bytes + 1)
            if len(payload) > max_bytes:
                raise IntegrationHttpError(f"integration response exceeded {max_bytes} bytes")
            return payload
    except HTTPError as exc:
        payload = exc.read(20_001).decode("utf-8", errors="replace")
        retryable = exc.code in {408, 409, 425, 429} or exc.code >= 500
        raise IntegrationHttpError(
            f"integration request failed with HTTP {exc.code}: {payload[:20_000]}",
            retryable=retryable,
        ) from exc
    except URLError as exc:
        raise IntegrationHttpError(
            f"integration request failed: {exc.reason}",
            retryable=True,
        ) from exc
