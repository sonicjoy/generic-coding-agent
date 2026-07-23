# AGENTS.md

Guidance for humans and AI agents working in the `generic-coding-agent` repo.

## Overview

`generic-coding-agent` is a provider-agnostic, pluggable coding-agent harness.
The core loop, tools, sessions, patch engine, repository manifests, jobs,
integrations, `AGENTS.md` ingestion, skills, plugin loading, and containerized
command execution live under `src/gca/`. The optional ASGI API and worker live
under `src/gca_service/`. See `README.md` for the full layout and command
reference.

## Conventions

- Python, `src/` layout, package `gca`, console script `gca`.
- Prefer **uv** for installs (`uv sync --extra dev`).
- Keep imports at the top of modules; add docstrings to public functions.
- Filesystem tools must stay confined to the workspace via `ToolContext.resolve`.
- Target-repo commands must run through `ToolContext.executor` (Docker in real
  runs; `FakeExecutor` in unit tests). Never add a host-subprocess fallback for
  repo commands.
- Prefer small, targeted edits and unified diffs (`apply_patch`) over rewrites.

## Standard commands

Defined in `pyproject.toml` / `README.md`:

- Lint: `ruff check .`
- Format check: `ruff format --check .`
- Types: `mypy`
- Tests: `pytest`
- Docker smoke: `pytest -m docker` (requires Docker Engine)

## Cursor Cloud specific instructions

- The default product remains a pure Python CLI (`gca` / `python -m gca`). The
  optional `gca-service` extra provides a Starlette API, SQLite job store, and
  separate worker; it has no external database requirement for local testing.
- Prefer `uv sync --extra dev` (creates `.venv`). Activate with
  `. .venv/bin/activate` before running `gca`, `pytest`, `ruff`, or `mypy`.
  Pip/`python -m venv` remains supported as a fallback.
- **Docker Engine** is required for real `gca run` / job / worker command
  execution. Offline unit tests inject `FakeExecutor` and do not need Docker.
- Creating a venv without uv requires the system package `python3.12-venv`
  (installed via apt). If venv creation fails with an `ensurepip` error, run
  `sudo apt-get install -y python3.12-venv`.
- No LLM credentials are needed to develop or test. The harness is
  provider-agnostic: use the built-in scripted provider (`--script <file.json>`)
  for deterministic runs, or a plugin exposing `get_provider()` for a real model.
  The test suite and the demo run entirely offline when a fake executor is used;
  live demos that execute `run_command` need Docker.
- End-to-end demo (creates + patches + runs a file in a scratch workspace).
  The demo script calls `python3` via `run_command`, so seed a Python
  isolation image — the packaged default image is sandbox-only (no language
  SDKs):
  ```bash
  mkdir -p /tmp/gca_demo
  cp examples/templates/Dockerfile.agent /tmp/gca_demo/
  gca run "Add a greeting feature to this project" --workspace /tmp/gca_demo \
    --skills examples/skills --script examples/demo_script.json
  ```
- Sessions persist as JSON under `<workspace>/.gca/sessions` by default (ignored
  by git). Local runs also use ephemeral copies under `.gca/runs/` and sync
  changes back on success. List with `gca sessions` and continue with
  `gca resume <id>`.
- Service tests are offline and use Starlette's in-process test client, temporary
  Git repositories, scripted providers, and fake SCM adapters.
- Deploy artifacts: root `Dockerfile` (uv-based) and `compose.yaml` mount the
  host Docker socket for nested isolation containers.
