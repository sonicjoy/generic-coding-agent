# GCA: Generic Coding Agent

Provider-agnostic harness for autonomous coding agents. Point it at a task (or
SCM issue); it reasons, edits code, runs checks, and iterates until done. The
LLM backend is pluggable.

## What it does

```
observe -> reason (LLM) -> choose tool -> execute -> record result -> repeat
```

Stops when the model calls `finish`, returns no tool calls, or hits the step
budget.

**Core**

- Multi-step runs with an explicit step budget and resumable JSON sessions
- Multi-model routing (strength / speed / cost) and `fast` vs `feature`
  workflows (plan → implement → review)
- Tools: explore/search, filesystem CRUD, `apply_patch`, `search_replace`,
  `run_command` (Docker-isolated; destructive shell patterns blocked)
- `AGENTS.md` ingestion, skills (`SKILL.md`), plugins, `.gca/config.yaml`
  manifests, `models.yaml` catalogs

**Hosted (optional `gca-service`)**

- Durable SQLite jobs with leases, retries, resume, and GitHub/GitLab publish
- Verified webhooks, GitLab issue sessions, worker + authenticated API

## Install

Python 3.10+, [uv](https://docs.astral.sh/uv/), and **Docker Engine** for real
`run_command` runs (unit tests use a fake executor).

```bash
uv sync --extra dev          # or: uv sync --extra service
source .venv/bin/activate
gca --help
docker info                  # required for live agent command execution
```

Pip fallback: `python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"`.

## Quick start (target repo)

Templates live under `examples/templates/` (`AGENTS.md`, `models.yaml`,
`.gca/config.yaml`, personas, sample skill).

```bash
GCA=/path/to/generic-coding-agent
cd /path/to/your-project

cp "$GCA/examples/templates/models.yaml" ./models.yaml
cp "$GCA/examples/templates/AGENTS.md" ./AGENTS.md
mkdir -p .gca/personas
cp "$GCA/examples/templates/.gca/config.yaml" ./.gca/config.yaml
cp "$GCA/examples/templates/.gca/persona.md" ./.gca/persona.md
cp "$GCA/examples/templates/.gca/personas/"*.md ./.gca/personas/
cat "$GCA/examples/templates/gca.gitignore" >> ./.gitignore

printf 'OPENROUTER_API_KEY=sk-or-...\n' > .env && chmod 600 .env
gca validate --workspace .
gca run "Fix the flaky login test"
gca sessions
gca resume <session_id>
```

Model catalog search order (later wins): `~/.gca/models.yaml` →
`<project>/models.yaml` → `<project>/.gca/models.yaml` → `--models` (repeatable).
Store only env var *names* in YAML (`api_key_env`), never secrets. Prefer
`.env`, `~/.gca/.env`, or `.gca/.env` (gitignored).

| Commit | Do not commit |
|--------|----------------|
| `models.yaml`, `AGENTS.md`, `.gca/config.yaml`, personas, `skills/**` | `.env`, `.gca/.env`, `.gca/sessions/`, `.gca/jobs/`, API keys |

**Isolated job (clean clone):**

```bash
gca job run "Fix the null metadata bug" \
  --repository https://git.example.com/team/repo.git \
  --ref main --allowed-host git.example.com
gca job resume <job_id> --max-steps 80 --allowed-host git.example.com
```

### Step budget

Effective max steps (highest wins first):

1. Explicit `--max-steps` / `max_steps` on `POST /runs` or resume
2. `GCA_DEFAULT_MAX_STEPS` (hosted, when omitted)
3. Repo `.gca/config.yaml` `runtime.max_steps` (default `25`)
4. Feature workflows reserve review steps (`review_step_reserve`, default `5`)

Mid-run budget exhaustion **pauses** the job (no publish of unfinished
implementation). Resume with a larger `max_steps` or an authorized `/agent fix`.

## Repository manifest

`.gca/config.yaml` is the portable, strict repo config (`models.yaml` stays
separate). Precedence: code defaults → `~/.gca/config.yaml` → repo manifest →
`AGENTS.md` `gca:` frontmatter → CLI.

```yaml
version: 1
context:
  files: [AGENTS.md]
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
    planning: [strong-model, fallback-strong-model]
    implementation: fast-model
    review: [strong-model, fallback-strong-model]
  max_review_cycles: 2
  review_step_reserve: 5
runtime:
  max_steps: 25
  max_tool_timeout: 300
tools:
  deny: []
  fixed_commands:
    run_tests:
      description: Run the repository test suite.
      argv: [python, -m, pytest]
      phases: [execute, implementation, review]
publication:
  required_checks: [run_tests]
  allowed_paths: ["src/**", "tests/**"]
  denied_paths: [".env", ".gca/.env"]
  max_files: 50
  max_changed_lines: 2000
  auto_merge: false
```

Fixed commands run a fixed argv in the isolation container (no shell). Global
deny / phase allow rules are enforced by the tool registry.

Isolation containers default to `network: false`. Set
`environment.network: true` only when trusted fixed commands need outbound
access (treat as a trust-boundary change). Prefer a repo `Dockerfile.agent`
(see `examples/templates/Dockerfile.agent`); otherwise GCA uses a minimal
default image (bash/git/curl only).

## Providers

Declarative `models.yaml` is preferred; plugins are for custom tools or
non-OpenAI-compatible backends. Offline demos: `--script`.

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
```

Untrusted checkout inspection:

```bash
gca run "Inspect this repository" \
  --trusted-models-only \
  --models /path/to/operator-owned-models.yaml
```

Plugins may expose `get_models()` / `get_provider()` and custom tools. Hosted
workers refuse checkout-local plugins (`GCA_PLUGIN_DIR` only) and ignore
checkout-local model catalogs. Grant tool secrets explicitly:

```yaml
tools:
  secret_access:
    query_observability: [OBSERVABILITY_API_TOKEN]
```

```bash
export GCA_TOOL_SECRET_GRANTS='{"github.com/example/project":{"query_metrics":["METRICS_TOKEN"]}}'
```

## Workflows and routing

`gca` classifies tasks without an extra model call:

- **fast** — one efficient coding agent
- **feature** — planner → implementer → reviewer (up to two rework cycles)

Planning gets read/search tools; review can `run_command` but not edit;
implementation gets edit tools. Override with `--workflow fast|feature|auto`
or `routing` in the manifest.

Labeled SCM issue webhooks wrap title/body as untrusted text. Complexity
scoring for those framed tasks uses the **issue title only**, so process words
in the description do not force a feature workflow. Plain CLI tasks still
classify on the full prompt.

## Optional hosted service

```bash
uv sync --extra service   # or pip install -e ".[service]"
export GCA_API_TOKEN=replace-with-a-long-random-token
export GCA_DATA_DIR=/var/lib/gca
export GCA_ALLOWED_REPOSITORY_HOSTS=github.com,gitlab.com
export GCA_MODEL_CONFIG_PATHS=/etc/gca/models.yaml
gca-service serve --host 0.0.0.0 --port 8000
gca-service worker
```

Full env reference: `examples/service.env.example`.

### HTTP API

| Method | Path | Notes |
|--------|------|--------|
| `POST` | `/runs` | Authenticated `RunSpec`; supports `Idempotency-Key` |
| `GET` | `/runs/latest` | Newest job in the configured `GCA_DATA_DIR` |
| `GET` | `/runs/{id}` | Status, publication, lease fields, LLM usage/cost, session progress |
| `POST` | `/runs/{id}/cancel` | Cancel queued/running work |
| `POST` | `/runs/{id}/resume` | Larger `max_steps` after budget pause |
| `POST` | `/runs/{id}/requeue` | Operator reclaim of a leased job |
| `POST` | `/webhooks/github` | Verified GitHub ingress |
| `POST` | `/webhooks/gitlab/{registration_id}` | Preferred GitLab issue-agent ingress |
| `POST` | `/webhooks/gitlab` | Legacy single-registration GitLab |
| `*` | `/issue-sessions…` | List/create/get/events/transcript/cancel/retry |
| `GET` | `/health`, `/ready` | Liveness; `/ready` includes worker claim metadata |

`GCA_READY_WORKER_CLAIM_TIMEOUT_SECONDS` (default `0`) can make `/ready` fail
when queued jobs exist but no worker has claimed work in that window.

On SIGTERM/SIGINT the worker releases its active lease immediately so another
worker can claim; hard kills wait for `GCA_LEASE_SECONDS` or
`POST /runs/{id}/requeue`.

`GET /runs/{id}` exposes durable LLM usage (`llm_usage`, token counts,
`cost_usd`) and session progress when available. Keep API and worker on the
same durable `GCA_DATA_DIR`; startup logs the data dir and newest job id.

### Webhooks and publication

- **GitHub**: `GCA_GITHUB_WEBHOOK_SECRET` + `GCA_ALLOWED_GITHUB_PROJECTS`.
  Issue jobs enqueue when a maintainer applies `gca-run`
  (`GCA_GITHUB_TRIGGER_LABEL`). PR review / `/agent fix` and merged-PR cancel
  behavior are supported — see webhook event notes in
  `examples/service.env.example`.
- **Opt-in issue UX**: `GCA_GITHUB_ISSUE_ASSIGN` /
  `GCA_GITHUB_ISSUE_PROGRESS_COMMENTS` (needs `issues:write`).
- **Early branch**: labeled GitHub issue jobs create and push a working branch
  (and link it when the API allows) before the agent runs.
- **GitLab issue sessions**: prefer
  `GCA_GITLAB_WEBHOOK_REGISTRATIONS` →
  `POST /webhooks/gitlab/{registration_id}`. Start on trigger label or exact
  `/agent run`; commands `/agent run|fix|cancel|status`. Service-owned clone /
  commit / push / MR / merge — never granted to tools. Auto-merge is two-key
  (`GCA_ALLOW_AUTO_MERGE_PROJECTS` + repo `publication.auto_merge`).
- **Publish mode** (`GCA_PUBLISH_MODE`): `off` | `branch` | `pr` (`auto` ≡ `pr`).
  Publication targets require matching `GCA_GITHUB_TOKEN` /
  `GCA_GITLAB_TOKEN` at enqueue time. Tokens also provide askpass for private
  HTTPS clones; they never enter the agent subprocess.

Smoke test:

```bash
export GCA_API_TOKEN=... GCA_GITHUB_WEBHOOK_SECRET=...
export GCA_ALLOWED_GITHUB_PROJECTS=owner/repo
export GCA_E2E_REPOSITORY_FULL_NAME=owner/repo
export GCA_E2E_REPOSITORY_CLONE_URL=https://github.com/owner/repo.git
export GCA_DATA_DIR=/tmp/gca-e2e
examples/e2e_webhook.sh   # defaults GCA_PUBLISH_MODE=off
```

### Deploy

```bash
docker build -t gca:latest .
cp examples/service.env.example .env   # set GCA_* tokens
docker compose up --build              # API waits for /ready, then worker
```

Mount durable `GCA_DATA_DIR` and `/var/run/docker.sock`. Optional
`GCA_DOCKER_DISABLE_RESOURCE_LIMITS=1` on nested cgroupv2 hosts that reject
`--cpus`/`--memory`.

## Development

```bash
uv sync --extra dev --extra service
ruff check .
ruff format --check .
mypy
pytest                # unit + offline evals (no Docker)
pytest -m eval
pytest -m docker      # needs Docker Engine
```

Offline scripted demo (needs Docker for `run_command`):

```bash
mkdir -p /tmp/gca_demo
cp examples/templates/Dockerfile.agent /tmp/gca_demo/
gca run "Add a greeting feature to this project" --workspace /tmp/gca_demo \
  --skills examples/skills --script examples/demo_script.json
```

## Layout

```
src/gca/           CLI harness (agent, tools, jobs, executor, integrations…)
src/gca_service/   optional Starlette API + worker
examples/          templates, service.env.example, e2e_webhook.sh, demo script
evals/             offline scripted scenarios
tests/             pytest suite
Dockerfile         uv-based API/worker image
compose.yaml       local API+worker with docker.sock
```
