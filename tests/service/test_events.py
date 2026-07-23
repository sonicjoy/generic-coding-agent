from __future__ import annotations

from gca_service.events import structured_event


def test_structured_event_quotes_values_with_spaces() -> None:
    line = structured_event(
        "worker",
        "claim",
        job_id="abc",
        last_error="boom with spaces",
        empty="",
        missing=None,
    )

    assert line.startswith("[worker] event=claim job_id=abc")
    assert 'last_error="boom with spaces"' in line
    assert "empty=" not in line
    assert "missing=" not in line
