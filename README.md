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
- **Portable repository manifests**: strict `.gca/config.yaml` validation for
  personas, skills, routing, fixed commands, tool permissions, and limits.
- **Durable hosted jobs**: idempotent SQLite jobs, isolated clones, leases,
  retries, resume, and service-owned GitHub/GitLab publication.
- **Optional API/worker service**: authenticated runs and verified SCM webhooks
  without adding HTTP dependencies to the default installation.

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

Ready-to-copy templates under ``examples/templates/`` include `AGENTS.md`,
`models.yaml`, `.gca/config.yaml`, personas, and a sample skill.

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
mkdir -p .gca/personas
cp "$GCA/examples/templates/.gca/config.yaml" ./.gca/config.yaml
cp "$GCA/examples/templates/.gca/persona.md" ./.gca/persona.md
cp "$GCA/examples/templates/.gca/personas/"*.md ./.gca/personas/
cat "$GCA/examples/templates/gca.gitignore" >> ./.gitignore
```

Edit model IDs / scores in `models.yaml`, routing and fixed commands in
`.gca/config.yaml`, and project-specific conventions in `AGENTS.md`. Then
validate everything without making a model call:

```bash
gca validate --workspace .
```

Model catalog search order (later overrides earlier):

1. `~/.gca/models.yaml`
2. `<project>/models.yaml`
3. `<project>/.gca/models.yaml`
4. `--models <path>` (repeatable)

Never put API keys in `models.yaml` — only the env var *name* (for example
`api_key_env: OPENROUTER_API_KEY`). Model names under `routing.models` in
`.gca/config.yaml` must match names registered in `models.yaml`.

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
`--skills` directories. Each skill uses `skills/<name>/SKILL.md` with YAML
`name` and `description`; only its catalog entry is injected until the model
calls `load_skill`.

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
| `AGENTS.md`, `.gca/config.yaml` | `.gca/sessions/` |
| `.gca/persona.md`, `.gca/personas/**` | `.gca/jobs/` |
| `skills/**` | API keys |

Suggested layout after setup (mmmapper shown as the example project):

```text
mmmapper/
  AGENTS.md            # copied from examples/templates/
  models.yaml          # copied from examples/templates/
  .gca/
    config.yaml        # routing, tools, limits
    persona.md         # optional base persona
    personas/          # optional phase personas
    sessions/          # runtime, ignored
  .env                 # local only
  skills/              # optional
```

### 8. Durable isolated job run

Use the job runner when the harness should clone a clean repository instead of
working in the current checkout:

```bash
gca job run "Fix the null metadata bug" \
  --repository https://git.example.com/team/mmmapper.git \
  --ref main \
  --allowed-host git.example.com

# If the step budget pauses the job:
gca job resume <job_id> --max-steps 80 --allowed-host git.example.com
```

Jobs use isolated workspaces, SQLite-backed idempotency and leases, and the same
resumable agent sessions. Hosted-mode jobs hide raw `run_command` unless the
manifest explicitly exposes it; configure safe checks as fixed argv commands.

## Repository manifest

`.gca/config.yaml` is a strict, versioned manifest for settings that should be
portable with a repository. `models.yaml` remains separate.

```yaml
version: 1

context:
  files: [AGENTS.md, CLAUDE.md]
  include_frontmatter: false
  persona_file: .gca/persona.md
  phase_personas:
    planning: .gca/personas/planning.md
    implementation: .gca/personas/implementation.md
    review: .gca/personas/review.md

skills:
  dirs: [.gca/skills, skills]

routing:
  workflow: auto
  models:
    fast: fast-model
    planning: strong-model
    implementation: fast-model
    review: strong-model
  max_review_cycles: 2

runtime:
  profile: local
  max_steps: 25
  max_tool_timeout: 300

tools:
  deny: []
  fixed_commands:
    run_tests:
      description: Run the repository test suite.
      argv: [python, -m, pytest]
      cwd: .
      timeout: 300
      phases: [execute, implementation, review]
```

Effective precedence is code defaults → `~/.gca/config.yaml` → repository
`.gca/config.yaml` → nested `AGENTS.md` `gca:` frontmatter → CLI flags.
Manifest paths are repository-relative and cannot escape the workspace.
Frontmatter remains backward-compatible but is stripped from model-facing
instructions by default.

Fixed commands use `subprocess` argv with `shell=False`; optional parameters
must declare bounded types/choices in the manifest. Global deny and phase allow
rules are enforced by the tool registry, not only by prompts.

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

Local runs treat workspace model catalogs as trusted repository configuration.
When inspecting an untrusted checkout, ignore its catalogs and provider secret
selectors:

```bash
gca run "Inspect this repository" \
  --trusted-models-only \
  --models /path/to/operator-owned-models.yaml
```

### Optional plugins

- ``get_models()`` / ``get_provider()`` can still register models; plugin names
  override YAML entries with the same name.
- Plugins are also used for custom tools.
- Local repository plugins are trusted in-process Python. Hosted workers refuse
  checkout-local plugins and load only an operator-supplied external directory.
- Approved tools request secrets through `ToolContext.secret`; authorize each
  environment variable explicitly in `.gca/config.yaml`:
- Read-only plugin tools should declare `capabilities = frozenset({"read_external"})`
  before they can be exposed to planning or review phases.

```yaml
tools:
  secret_access:
    query_observability: [OBSERVABILITY_API_TOKEN]
```

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
`routing` in `.gca/config.yaml`. Model values refer to names registered in
`models.yaml` or plugins. Legacy YAML frontmatter remains supported:

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
files override individual routing values. Markdown bodies remain part of every
agent's system context; configuration frontmatter is stripped by default.

## Optional hosted service

Install the service extra, then run the API and worker as separate processes
sharing the same `GCA_DATA_DIR`. See `examples/service.env.example` for all
settings:

```bash
pip install -e ".[service]"

export GCA_API_TOKEN=replace-with-a-long-random-token
export GCA_DATA_DIR=/var/lib/gca
export GCA_ALLOWED_REPOSITORY_HOSTS=github.com,gitlab.com
export GCA_MODEL_CONFIG_PATHS=/etc/gca/models.yaml

gca-service serve --host 0.0.0.0 --port 8000
gca-service worker
```

The API exposes:

- `POST /runs` — authenticated generic `RunSpec` submission; supports
  `Idempotency-Key`.
- `GET /runs/{id}`, `POST /runs/{id}/cancel`, and
  `POST /runs/{id}/resume` with a larger `max_steps` budget.
- `POST /webhooks/github` and `POST /webhooks/gitlab`.
- `GET /health` and `GET /ready`.

Example run:

```bash
curl -X POST http://localhost:8000/runs \
  -H "Authorization: Bearer $GCA_API_TOKEN" \
  -H "Idempotency-Key: ticket-123" \
  -H "Content-Type: application/json" \
  -d '{
    "task": "Fix the flaky login test",
    "repository": {
      "url": "https://github.com/example/project.git",
      "ref": "main"
    },
    "workflow": "auto"
  }'
```

GitHub webhooks require `GCA_GITHUB_WEBHOOK_SECRET` and an explicit
`GCA_ALLOWED_GITHUB_PROJECTS=owner/repo,...` allowlist. GitLab uses
`GCA_GITLAB_WEBHOOK_SECRET` and `GCA_ALLOWED_GITLAB_PROJECTS=group/repo,...`.
The service verifies every delivery before normalization and deduplicates its
authenticated body even if a replay uses a new delivery ID. Issues enqueue only
when a maintainer applies the `gca-run` label; customize it with
`GCA_GITHUB_TRIGGER_LABEL` / `GCA_GITLAB_TRIGGER_LABEL`.

For publication, set repository-scoped `GCA_GITHUB_TOKEN` and/or
`GCA_GITLAB_TOKEN`. These tokens also provide temporary askpass credentials for
private HTTPS clones on `GCA_GITHUB_HOST` / `GCA_GITLAB_HOST`; they are never
passed to the agent subprocess. The worker—not the LLM—runs required fixed
checks, enforces the repository's `publication` limits, commits, pushes a
deterministic branch, and opens an idempotent PR/MR:

```yaml
publication:
  required_checks: [run_tests]
  allowed_paths: ["src/**", "tests/**", "README.md"]
  denied_paths: [".env", ".gca/.env"]
  max_files: 50
  max_changed_lines: 2000
```

The bundled SQLite store supports a single node with multiple worker processes
and serializes jobs targeting the same repository. Deployments with ephemeral
filesystems or horizontal nodes must provide durable shared storage and a
`JobStore`/`JobQueue` backend appropriate to that platform. Lambda is suitable
as ingress, not for the long-running worker.

Hosted jobs never import Python plugins from the cloned repository; pass an
operator-installed `GCA_PLUGIN_DIR` instead. They also ignore checkout-local
model catalogs and use `~/.gca/models.yaml` plus operator-owned
`GCA_MODEL_CONFIG_PATHS`; this prevents a repository from selecting a service
secret as its provider key. Repository tool secret requests are denied unless
the exact canonical project, tool, and environment name are granted in
`GCA_TOOL_SECRET_GRANTS`; API, webhook, and SCM tokens cannot be granted.
Publication uses the immutable manifest snapshot loaded before the agent runs.

```bash
export GCA_TOOL_SECRET_GRANTS='{
  "github.com/example/project": {
    "query_metrics": ["METRICS_TOKEN"]
  }
}'
```

The built-in controls are not an OS sandbox: deploy workers in isolated
containers with filesystem, resource, and network-egress limits. Monitoring /
anomaly detection stays outside core and can create an SCM issue or call
`POST /runs`.

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
pip install -e ".[dev,service]"
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
  repo_config.py versioned .gca/config.yaml loader
  tool_policy.py phase-aware tool exposure
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
  jobs/          durable job lifecycle, SQLite store/queue, runner
  workspace/     isolated repository preparation
  integrations/  webhook and SCM adapter contracts/implementations
  providers/     LLMProvider, OpenAI-compatible, ScriptedProvider
  tools/         built-in and fixed-command tools
src/gca_service/ optional ASGI API and worker
examples/        copy-ready config/persona/skill/model templates
evals/           offline deterministic evaluation scenarios
tests/           pytest suite
```
