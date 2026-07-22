from __future__ import annotations

import json
from pathlib import Path

from gca.cli import main


def test_cli_runs_fast_scripted_workflow(tmp_path: Path) -> None:
    script_path = tmp_path / "script.json"
    script_path.write_text(
        json.dumps(
            [
                {
                    "tool_calls": [
                        {
                            "name": "finish",
                            "arguments": {"summary": "CLI workflow completed."},
                        }
                    ]
                }
            ]
        ),
        encoding="utf-8",
    )

    result = main(
        [
            "run",
            "Fix a typo",
            "--workspace",
            str(tmp_path),
            "--sessions-dir",
            str(tmp_path / "sessions"),
            "--script",
            str(script_path),
            "--workflow",
            "fast",
        ]
    )

    assert result == 0
