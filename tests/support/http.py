"""HTTP response doubles used when monkeypatching urllib openers."""

from __future__ import annotations

import json
from typing import Any


class FakeResponse:
    """Context-manager response with ``read()`` and ``headers`` like urllib."""

    def __init__(
        self,
        payload: dict[str, Any] | bytes | str,
        headers: dict[str, str] | None = None,
    ) -> None:
        if isinstance(payload, bytes):
            self._payload = payload
        elif isinstance(payload, str):
            self._payload = payload.encode()
        else:
            self._payload = json.dumps(payload).encode()
        self.headers = dict(headers or {})

    def read(self, size: int = -1) -> bytes:
        return self._payload[:size] if size >= 0 else self._payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None
