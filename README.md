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
- **Multi-model routing** using registered strength, speed, cost, and capability
  metadata.
- **Workflow orchestration**: small tasks use one efficient agent; feature work
  uses separate planning, implementation, and review agents.
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

The harness ships no built-in network LLM client. Provide one of:

- A **plugin** module exposing `get_provider() -> LLMProvider` (see
  `gca.providers.base.LLMProvider`), passed via `--plugins <dir>`. This legacy
  hook remains supported as a single balanced model.
- A **multi-model plugin** exposing `get_models()`, returning named
  `ModelProfile` objects. Each profile wraps a configured `LLMProvider` and
  scores its strength, speed, and cost from 1–5:

```python
from gca.models import ModelProfile


def get_models():
    return [
        ModelProfile(
            name="fast",
            provider=MyProvider(model="fast-model"),
            strength=2,
            speed=5,
            cost=1,
        ),
        ModelProfile(
            name="strong",
            provider=MyProvider(model="strong-model"),
            strength=5,
            speed=2,
            cost=5,
        ),
    ]
```

  Provider plugins create API clients and read credentials from environment
  variables. Do not put credentials in `AGENTS.md`.
- The built-in **scripted provider** (`--script script.json`) for demos/tests,
  which replays a fixed sequence of tool calls with no network access.

## Workflows and routing

`gca` classifies task text deterministically, without an extra model call:

- `fast`: one efficient coding agent for small tasks.
- `feature`: a strong planning agent, an efficient implementation agent, then
  an independent strong reviewer. Reviewers can request up to two rework cycles.

Planning receives only read/search tools. Review also receives `run_command`
for verification but no file-edit tools; only implementation agents receive
the file-editing tools. Each role has a separate conversation, and structured
plans/reviews are persisted in the parent session.

Override automatic selection with `--workflow fast|feature|auto`, or configure
the repository through optional YAML frontmatter in `AGENTS.md`. Model values
refer to names registered by plugins:

```yaml
---
gca:
  workflow: auto
  models:
    fast: fast
    planning: strong
    implementation: fast
    review: strong
  minimum_strength:
    implementation: 2
  max_review_cycles: 2
  complexity:
    feature_threshold: 3
    large_threshold: 6
---
```

Nested `AGENTS.md`/`CLAUDE.md` configuration is merged root-first, so deeper
files override individual values. Existing Markdown instructions remain part
of every agent's system context.

## Usage

```bash
# Run a task using a scripted provider and the example word_count plugin
gca run "Create hello.py" --script script.json --plugins examples/plugins

# Force the multi-agent feature workflow
gca run "Add search history" --plugins path/to/plugins --workflow feature

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
  models.py      named model profiles + selection
  routing.py     AGENTS.md routing policy
  complexity.py  deterministic workflow classification
  workflows.py   built-in workflow definitions
  orchestrator.py multi-agent workflow coordinator
  skills.py      skill discovery + load_skill tool
  plugins.py     dynamic plugin loading
  providers/     LLMProvider interface + ScriptedProvider
  tools/         built-in tools (filesystem, search, patch, shell, control)
examples/        example skill + plugin
tests/           pytest suite
```
