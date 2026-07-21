# generic-coding-agent

A provider-agnostic, pluggable **autonomous coding-agent harness** — the core
engine you point at a task (or a git issue) so it can reason, edit code, run
tests, and iterate until the work is done. It is deliberately unopinionated about
the LLM backend: you plug in your own provider, tools, and skills.

## What it does

The harness runs an agentic loop:

```
observe -> reason (LLM) -> choose tool -> execute -> record result -> update plan -> repeat
```

It stops when the model calls the `finish` tool, returns no further tool calls,
or the step budget is exhausted.

### Capabilities

- **Multi-step reasoning** with an explicit step budget.
- **Memory / state** persisted per session (resume any run).
- **Batteries-included tools**: `explore`, `search`, `read_file`, `write_file`,
  `create_file`, `delete_file`, `move_file`, `apply_patch` (unified diffs), and
  `run_command` (tests, linters, formatters, builds, dev servers, analysis).
- **Safe patching**: unified diffs are validated then applied atomically; on any
  failure nothing is written.
- **`AGENTS.md` ingestion**: project instructions are discovered (nested,
  root-first) and injected into the system prompt.
- **Skills**: `SKILL.md` SOP files are indexed and lazily loaded via `load_skill`.
- **Plugins**: drop-in Python modules add tools or wire in an LLM provider — no
  build step.
- **Sessions**: create / list / resume, persisted as JSON.

## Install (development)

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

## Provider configuration

The harness ships no built-in LLM client. Provide one of:

- A **plugin** module exposing `get_provider() -> LLMProvider` (see
  `gca.providers.base.LLMProvider`), passed via `--plugins <dir>`.
- The built-in **scripted provider** (`--script script.json`) for demos/tests,
  which replays a fixed sequence of tool calls with no network access.

## Usage

```bash
# Run a task using a scripted provider and the example word_count plugin
gca run "Create hello.py" --script script.json --plugins examples/plugins

# List and resume sessions
gca sessions
gca resume <session_id> --script script.json
```

## Development

```bash
ruff check .          # lint
ruff format --check . # format check
mypy                  # type check
pytest                # tests
```

## Layout

```
src/gca/
  agent.py       core loop
  runtime.py     assembly (system prompt, registry, provider resolution)
  session.py     session persistence
  context.py     AGENTS.md discovery/merge
  skills.py      skill discovery + load_skill tool
  plugins.py     dynamic plugin loading
  providers/     LLMProvider interface + ScriptedProvider
  tools/         built-in tools (filesystem, search, patch, shell, control)
examples/        example skill + plugin
tests/           pytest suite
```
