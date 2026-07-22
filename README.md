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
- **Shell guardrails**: `run_command` blocks destructive commands (`rm`/`rmdir`/
  `unlink`, `sudo`, `git push --force`, `git reset --hard`, `git clean -f`,
  and similar) before they reach the shell.
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

For day-to-day use against other repos, ``pip install -e .`` (without ``[dev]``)
is enough.

## Quick start in your project

Use these steps to point `gca` at a repo you want the agent to improve. The
examples below use **mmmapper** as the sample workspace — replace that path
with yours.

Ready-to-copy templates:

- ``examples/templates/AGENTS.md``
- ``examples/templates/models.yaml``

### 1. Install the harness

```bash
cd /path/to/generic-coding-agent
python -m venv .venv
. .venv/bin/activate
pip install -e .
gca --help
```

Keep this environment activated (or install into a tooling venv on your `PATH`).

### 2. Enter your project

```bash
cd /path/to/mmmapper
```

`--workspace` defaults to the current directory.

### 3. Copy the templates

```bash
GCA=/path/to/generic-coding-agent
cp "$GCA/examples/templates/models.yaml" ./models.yaml
cp "$GCA/examples/templates/AGENTS.md" ./AGENTS.md
```

Edit model IDs / scores in `models.yaml` and project-specific conventions in
`AGENTS.md` as needed. Catalog search order (later overrides earlier):

1. `~/.gca/models.yaml`
2. `<project>/models.yaml`
3. `<project>/.gca/models.yaml`
4. `--models <path>` (repeatable)

Never put API keys in `models.yaml` — only the env var *name* (for example
`api_key_env: OPENROUTER_API_KEY`). Model names under `gca.models` in
`AGENTS.md` must match names registered in `models.yaml`.

### 4. Set the API key

```bash
printf 'OPENROUTER_API_KEY=sk-or-...\n' > .env
chmod 600 .env
```

Also supported: `~/.gca/.env`, `<project>/.gca/.env`, or a normal shell
`export`. Keep `.env` out of git.

### 5. (Optional) Add skills

```text
mmmapper/
  skills/
    my-workflow/
      SKILL.md
```

Skills are discovered from `skills/` and `.gca/skills/`, or from extra
`--skills` directories.

### 6. Run a task

```bash
. /path/to/generic-coding-agent/.venv/bin/activate
cd /path/to/mmmapper

gca run "Fix the flaky login test"
gca run "Fix a typo in README" --workflow fast
gca run "Add search history to the API" --workflow feature
gca run "Refactor auth middleware" --max-steps 40
```

- Small tasks use one efficient agent (`fast`).
- Feature/large changes use planner → implementer → reviewer (`feature`).
- Sessions are stored under `.gca/sessions/` (gitignored).

### 7. List and resume sessions

```bash
gca sessions
gca resume <session_id>
```

### What to commit

| Commit | Do not commit |
|--------|----------------|
| `models.yaml` | `.env`, `.gca/.env` |
| `AGENTS.md` | `.gca/sessions/` |
| `skills/**` | API keys |

Suggested layout after setup (mmmapper shown as the example project):

```text
mmmapper/
  AGENTS.md            # copied from examples/templates/
  models.yaml          # copied from examples/templates/
  .env                 # local only
  skills/              # optional
  .gca/sessions/       # runtime, ignored
```

### Unattended / server runs

```bash
#!/usr/bin/env bash
set -euo pipefail
. /opt/gca/.venv/bin/activate
cd /srv/repos/mmmapper
set -a; . ./.env; set +a
gca run "Pick up the next failing CI issue and fix it" \
  --workflow auto \
  --max-steps 50
```

Destructive shell commands (`rm`, `git push --force`, etc.) are hard-blocked;
intentional deletes go through `delete_file`. There is no human approval loop.

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

See ``examples/templates/models.yaml`` (or ``examples/models.yaml``) for a
fuller OpenRouter catalog.

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

See [Quick start in your project](#quick-start-in-your-project) for the full
setup flow (examples use mmmapper). Short examples:

```bash
# After models.yaml + .env are in place
gca run "Fix a typo in README"
gca run "Add search history" --workflow feature

# Offline demo with the scripted provider
gca run "Create hello.py" --script examples/demo_script.json --plugins examples/plugins
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
