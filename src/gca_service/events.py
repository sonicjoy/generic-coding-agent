"""Structured stdout events for the hosted worker (always flushed by the CLI)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

EventSink = Callable[[str], None]


def structured_event(channel: str, event: str, **fields: Any) -> str:
    """Format a greppable ``[channel] event=... key=value`` worker log line."""

    parts = [f"[{channel}]", f"event={event}"]
    for key, value in fields.items():
        if value is None:
            continue
        text = str(value).replace("\n", " ").strip()
        if not text:
            continue
        if any(character in text for character in (" ", "=", '"')):
            text = json.dumps(text)
        parts.append(f"{key}={text}")
    return " ".join(parts)
