"""Small JSON HTTP client shared by optional SCM adapters."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class IntegrationHttpError(RuntimeError):
    """Raised when an integration API request fails."""


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> Any:
    """Issue one bounded JSON request."""

    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(
        url,
        data=data,
        headers={"Accept": "application/json", "Content-Type": "application/json", **headers},
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL is adapter-owned.
            payload = response.read(2_000_001)
            if len(payload) > 2_000_000:
                raise IntegrationHttpError("integration response exceeded 2 MB")
            return json.loads(payload.decode("utf-8")) if payload else None
    except HTTPError as exc:
        payload = exc.read(20_001).decode("utf-8", errors="replace")
        raise IntegrationHttpError(
            f"integration request failed with HTTP {exc.code}: {payload[:20_000]}"
        ) from exc
    except URLError as exc:
        raise IntegrationHttpError(f"integration request failed: {exc.reason}") from exc
