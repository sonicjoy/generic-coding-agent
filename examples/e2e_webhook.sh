#!/usr/bin/env bash
set -euo pipefail

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing required command: $1" >&2
    exit 2
  fi
}

require_env() {
  if [ -z "${!1:-}" ]; then
    echo "missing required environment variable: $1" >&2
    exit 2
  fi
}

json_get() {
  python - "$1" "$2" <<'PY'
import json
import sys

path, key = sys.argv[1], sys.argv[2]
with open(path, "r", encoding="utf-8") as handle:
    data = json.load(handle)
value = data
for part in key.split("."):
    value = value[part]
print(value)
PY
}

require_cmd curl
require_cmd python

GCA_SERVICE_BIN="${GCA_SERVICE_BIN:-gca-service}"
require_cmd "$GCA_SERVICE_BIN"

require_env GCA_API_TOKEN
require_env GCA_GITHUB_WEBHOOK_SECRET
require_env GCA_ALLOWED_GITHUB_PROJECTS
require_env GCA_E2E_REPOSITORY_FULL_NAME
require_env GCA_E2E_REPOSITORY_CLONE_URL

export GCA_PUBLISH_MODE="${GCA_PUBLISH_MODE:-off}"
export GCA_DATA_DIR="${GCA_DATA_DIR:-$(mktemp -d /tmp/gca-e2e-data.XXXXXX)}"
export GCA_GITHUB_TRIGGER_LABEL="${GCA_GITHUB_TRIGGER_LABEL:-gca-run}"
GCA_E2E_HOST="${GCA_E2E_HOST:-127.0.0.1}"
GCA_E2E_PORT="${GCA_E2E_PORT:-8000}"
GCA_E2E_API_URL="${GCA_E2E_API_URL:-http://${GCA_E2E_HOST}:${GCA_E2E_PORT}}"
GCA_E2E_TIMEOUT_SECONDS="${GCA_E2E_TIMEOUT_SECONDS:-300}"
GCA_E2E_POLL_SECONDS="${GCA_E2E_POLL_SECONDS:-2}"
GCA_E2E_DELIVERY_ID="${GCA_E2E_DELIVERY_ID:-gca-e2e-$(date +%s)}"

export GCA_ALLOWED_REPOSITORY_HOSTS="${GCA_ALLOWED_REPOSITORY_HOSTS:-$(python - <<'PY'
import os
from urllib.parse import urlparse

url = os.environ["GCA_E2E_REPOSITORY_CLONE_URL"]
host = urlparse(url).hostname
if not host:
    raise SystemExit("GCA_E2E_REPOSITORY_CLONE_URL must be an HTTPS clone URL")
print(host)
PY
)}"

case ",${GCA_ALLOWED_GITHUB_PROJECTS}," in
  *",${GCA_E2E_REPOSITORY_FULL_NAME},"*) ;;
  *)
    echo "GCA_ALLOWED_GITHUB_PROJECTS must include ${GCA_E2E_REPOSITORY_FULL_NAME}" >&2
    exit 2
    ;;
esac

tmp_dir="$(mktemp -d /tmp/gca-e2e-webhook.XXXXXX)"
api_log="${GCA_E2E_API_LOG:-${GCA_DATA_DIR}/e2e-api.log}"
worker_log="${GCA_E2E_WORKER_LOG:-${GCA_DATA_DIR}/e2e-worker.log}"
mkdir -p "$GCA_DATA_DIR"

api_pid=""
worker_pid=""
cleanup() {
  if [ -n "$worker_pid" ] && kill -0 "$worker_pid" 2>/dev/null; then
    kill "$worker_pid" 2>/dev/null || true
    wait "$worker_pid" 2>/dev/null || true
  fi
  if [ -n "$api_pid" ] && kill -0 "$api_pid" 2>/dev/null; then
    kill "$api_pid" 2>/dev/null || true
    wait "$api_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT

"$GCA_SERVICE_BIN" serve --host "$GCA_E2E_HOST" --port "$GCA_E2E_PORT" >"$api_log" 2>&1 &
api_pid="$!"

ready_deadline=$((SECONDS + 60))
until curl -fsS "${GCA_E2E_API_URL}/ready" >/dev/null 2>&1; do
  if [ "$SECONDS" -ge "$ready_deadline" ]; then
    echo "API did not become ready; log: $api_log" >&2
    exit 1
  fi
  sleep 1
done

"$GCA_SERVICE_BIN" worker >"$worker_log" 2>&1 &
worker_pid="$!"

body_file="${tmp_dir}/github-issues-labeled.json"
sig_file="${tmp_dir}/signature.txt"
python - "$body_file" "$sig_file" <<'PY'
import hashlib
import hmac
import json
import os
import sys

body_path, sig_path = sys.argv[1], sys.argv[2]
payload = {
    "action": "labeled",
    "label": {"name": os.environ["GCA_GITHUB_TRIGGER_LABEL"]},
    "repository": {
        "full_name": os.environ["GCA_E2E_REPOSITORY_FULL_NAME"],
        "clone_url": os.environ["GCA_E2E_REPOSITORY_CLONE_URL"],
        "default_branch": os.environ.get("GCA_E2E_DEFAULT_BRANCH", "main"),
    },
    "issue": {
        "number": int(os.environ.get("GCA_E2E_ISSUE_NUMBER", "999001")),
        "title": os.environ.get("GCA_E2E_ISSUE_TITLE", "E2E webhook smoke test"),
        "body": os.environ.get(
            "GCA_E2E_ISSUE_BODY",
            "Validate hosted GitHub webhook ingestion and worker execution.",
        ),
    },
}
body = json.dumps(payload, separators=(",", ":")).encode()
signature = hmac.new(
    os.environ["GCA_GITHUB_WEBHOOK_SECRET"].encode(),
    body,
    hashlib.sha256,
).hexdigest()
with open(body_path, "wb") as handle:
    handle.write(body)
with open(sig_path, "w", encoding="utf-8") as handle:
    handle.write(f"sha256={signature}")
PY

webhook_response="${tmp_dir}/webhook-response.json"
webhook_status="$(
  curl -sS -o "$webhook_response" -w "%{http_code}" \
    -X POST "${GCA_E2E_API_URL}/webhooks/github" \
    -H "X-GitHub-Event: issues" \
    -H "X-GitHub-Delivery: ${GCA_E2E_DELIVERY_ID}" \
    -H "X-Hub-Signature-256: $(cat "$sig_file")" \
    -H "Content-Type: application/json" \
    --data-binary "@${body_file}"
)"

if [ "$webhook_status" != "202" ]; then
  echo "webhook failed with HTTP ${webhook_status}" >&2
  cat "$webhook_response" >&2
  exit 1
fi

job_id="$(json_get "$webhook_response" id)"
echo "enqueued job_id=${job_id}"

run_response="${tmp_dir}/run-response.json"
deadline=$((SECONDS + GCA_E2E_TIMEOUT_SECONDS))
while true; do
  curl -fsS -o "$run_response" \
    -H "Authorization: Bearer ${GCA_API_TOKEN}" \
    "${GCA_E2E_API_URL}/runs/${job_id}"
  status="$(json_get "$run_response" status)"
  echo "job ${job_id} status=${status}"
  case "$status" in
    completed|failed|paused|cancelled)
      cat "$run_response"
      echo
      exit 0
      ;;
  esac
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "timed out waiting for terminal status; API log: $api_log worker log: $worker_log" >&2
    exit 1
  fi
  sleep "$GCA_E2E_POLL_SECONDS"
done
