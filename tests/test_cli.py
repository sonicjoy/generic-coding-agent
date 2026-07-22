from __future__ import annotations

import json
from pathlib import Path

from gca.cli import _build_config, _load_models, build_parser, main


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


def test_cli_loads_models_yaml_without_plugins(
    tmp_path: Path, monkeypatch: object
) -> None:
    catalog = tmp_path / "models.yaml"
    catalog.write_text(
        """
providers:
  local:
    type: openai_compatible
    base_url: https://example.test/v1
    api_key_env: TEST_MODELS_KEY
models:
  cheap:
    provider: local
    model_id: cheap-model
    strength: 2
    speed: 5
    cost: 1
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_MODELS_KEY", "secret")

    args = build_parser().parse_args(
        [
            "run",
            "task",
            "--workspace",
            str(tmp_path),
            "--models",
            str(catalog),
        ]
    )
    loaded = _load_models(args, _build_config(args))
    assert loaded.models.names() == ["cheap"]
