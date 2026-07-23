# AGENTS.md

Guidance for humans and AI agents working in the `generic-coding-agent` repo.

## Overview

`generic-coding-agent` is a provider-agnostic, pluggable coding-agent harness.
The core loop, tools, sessions, patch engine, repository manifests, jobs,
integrations, `AGENTS.md` ingestion, skills, and plugin loading live under
`src/gca/`. The optional ASGI API and worker live under `src/gca_service/`. See
`README.md` for the full layout and command reference.

## Conventions

- Python, `src/` layout, package `gca`, console script `gca`.
- Keep imports at the top of modules; add docstrings to public functions.
- Filesystem tools must stay confined to the workspace via `ToolContext.resolve`.
- Prefer small, targeted edits and unified diffs (`apply_patch`) over rewrites.

## Standard commands

Defined in `pyproject.toml` / `README.md`:

- Lint: `ruff check .`
- Format check: `ruff format --check .`
- Types: `mypy`
- Tests: `pytest`

## Cursor Cloud specific instructions

- The default product remains a pure Python CLI (`gca` / `python -m gca`). The
  optional `gca-service` extra provides a Starlette API, SQLite job store, and
  separate worker; it has no external database requirement for local testing.
- A virtualenv at `.venv` is the expected dev environment; activate it with
  `. .venv/bin/activate` before running `gca`, `pytest`, `ruff`, or `mypy`. The
  startup update script (re)creates `.venv` and installs the package editable
  with dev extras.
- Creating the venv requires the system package `python3.12-venv` (installed via
  apt). It is not reinstalled by the update script; if venv creation ever fails
  with an `ensurepip` error, run `sudo apt-get install -y python3.12-venv`.
- No LLM credentials are needed to develop or test. The harness is
  provider-agnostic: use the built-in scripted provider (`--script <file.json>`)
  for deterministic runs, or a plugin exposing `get_provider()` for a real model.
  The test suite and the demo run entirely offline.
- End-to-end demo (creates + patches + runs a file in a scratch workspace):
  `gca run "Add a greeting feature to this project" --workspace /tmp/gca_demo \
   --skills examples/skills --script examples/demo_script.json`
- Sessions persist as JSON under `<workspace>/.gca/sessions` by default (ignored
  by git). List with `gca sessions` and continue with `gca resume <id>`.
- Service tests are offline and use Starlette's in-process test client, temporary
  Git repositories, scripted providers, and fake SCM adapters.
