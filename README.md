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
- **Plugins**: optional drop-in Python modules for custom tools or exotic
  providers — no build step. Everyday model setup uses ``models.yaml``.
- **Sessions**: create / list / resume, persisted as JSON.

## Install (development)

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

## Provider configuration

Configure models declaratively with ``models.yaml`` (preferred). Plugins remain
optional for custom tools or non-OpenAI-compatible backends. Offline demos can
still use ``--script``.

### models.yaml (no plugins required)

Search order (later overrides earlier):

1. ``~/.gca/models.yaml``
2. ``<workspace>/models.yaml``
3. ``<workspace>/.gca/models.yaml``
4. Extra paths from ``--models``

```yaml
providers:
  openrouter:
    type: openai_compatible
    base_url: https://openrouter.ai/api/v1
    api_key_env: OPENROUTER_API_KEY

models:
  gpt-5.6-luna:
    provider: openrouter
    model_id: openai/gpt-5.6-luna
    strength: 3
    speed: 5
    cost: 1
  claude-opus-4.8:
    provider: openrouter
    model_id: anthropic/claude-opus-4.8
    strength: 5
    speed: 2
    cost: 5
```

API keys stay in environment variables (or a local ``.env`` / ``~/.gca/.env`` /
``<workspace>/.env`` file that is never committed):

```bash
export OPENROUTER_API_KEY=...
gca run "Fix a typo in README" --workspace .
```

See ``examples/models.yaml`` for a fuller OpenRouter catalog.

### Optional plugins

- ``get_models()`` / ``get_provider()`` can still register models; plugin names
  override YAML entries with the same name.
- Plugins are also used for custom tools.

```python
from gca.models import ModelProfile


def get_models():
    return [
        ModelProfile(
            name="custom",
            provider=MyProvider(model="custom-model"),
            strength=4,
            speed=3,
            cost=3,
        ),
    ]
```

### Scripted provider

The built-in **scripted provider** (``--script script.json``) replays a fixed
sequence of tool calls with no network access (demos/tests).

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
refer to names registered in ``models.yaml`` or plugins:

```yaml
---
gca:
  workflow: auto
  models:
    fast: gpt-5.6-luna
    planning: claude-opus-4.8
    implementation: gpt-5.6-luna
    review: claude-fable-5
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
# Run with models.yaml + env key (no plugins required)
export OPENROUTER_API_KEY=...
cp examples/models.yaml ./models.yaml
gca run "Fix a typo in README"

# Run a task using a scripted provider and the example word_count plugin
gca run "Create hello.py" --script script.json --plugins examples/plugins

# Force the multi-agent feature workflow
gca run "Add search history" --workflow feature
```

## Development

```bash
ruff check .          # lint
ruff format --check . # format check
mypy                  # type check
pytest                # unit + offline evals
pytest -m eval        # evaluation scenarios only
```

Offline eval scenarios live under ``evals/scenarios/`` and are driven by
scripted models (no network). Add a YAML scenario there to cover a new
workflow or routing behavior.

## Layout

```
src/gca/
  agent.py       core loop
  runtime.py     assembly (system prompt, registry, provider resolution)
  session.py     session persistence
  context.py     AGENTS.md discovery/merge
  models.py      named model profiles + selection
  model_config.py models.yaml catalog loader
  routing.py     AGENTS.md routing policy
  complexity.py  deterministic workflow classification
  workflows.py   built-in workflow definitions
  orchestrator.py multi-agent workflow coordinator
  skills.py      skill discovery + load_skill tool
  plugins.py     dynamic plugin loading
  providers/     LLMProvider, OpenAI-compatible, ScriptedProvider
  tools/         built-in tools (filesystem, search, patch, shell, control)
examples/        example skill, plugin, and models.yaml
evals/           offline deterministic evaluation scenarios
tests/           pytest suite
```
