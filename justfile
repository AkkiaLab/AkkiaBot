# AkkiaBot task runner. Run `just` to list recipes.

set shell := ["bash", "-euo", "pipefail", "-c"]

compose_dev := "docker compose -f deploy/compose.dev.yaml"

[private]
default:
    @just --list

# Install Python + web dependencies and git hooks
setup: setup-py setup-web
    @if git rev-parse --git-dir >/dev/null 2>&1; then uv run pre-commit install || echo "warning: pre-commit hooks not installed"; fi

setup-py:
    uv sync --all-packages --locked

setup-web:
    pnpm install --frozen-lockfile

# Lint everything (ruff, import-linter, biome)
lint: lint-py lint-web

lint-py:
    uv run ruff check .
    uv run ruff format --check .
    uv run lint-imports

lint-web:
    pnpm exec biome check .

# Scan every commit reachable from HEAD for credentials (same as the CI `secret-scan` job)
secret-scan:
    uv run pre-commit run gitleaks-history --hook-stage manual --all-files

# Auto-format and apply safe fixes
fmt:
    uv run ruff check --fix .
    uv run ruff format .
    pnpm exec biome check --write .

# Static type checks (pyright, tsc)
typecheck: typecheck-py typecheck-web

typecheck-py:
    uv run pyright

typecheck-web:
    pnpm -r typecheck

# Run all tests (pytest, vitest)
test: test-py test-web

test-py *args:
    uv run pytest {{ args }}

test-web:
    pnpm -r test

# Production build of the Mini App, then check it carries no dev-only Telegram mock
build-web:
    pnpm --filter @akkiabot/webapp build
    bash apps/webapp/scripts/check-no-dev-mock.sh apps/webapp/dist

# Run pytest against the compose postgres/redis (start them with `just dev` or `just services`)
test-integration *args:
    AKKIABOT_TEST_DATABASE_URL="postgresql+asyncpg://akkiabot:akkiabot@127.0.0.1:5432/akkiabot" \
    AKKIABOT_TEST_REDIS_URL="redis://127.0.0.1:6379/15" \
    uv run pytest {{ args }}

# Apply database migrations to DB_URL (reads .env)
migrate revision="head":
    uv run akkiabot migrate {{ revision }}

# Run the bot and HTTP API (reads .env; run `just migrate` first)
serve:
    uv run akkiabot serve

# Autogenerate a new Alembic revision against DB_URL (database must be at head)
db-revision message:
    uv run python -c 'import sys; from akkiabot_core.config import load_database_settings; from akkiabot_core.db.migrate import revision; revision(load_database_settings().url, sys.argv[1])' "{{ message }}"

# Start local postgres/redis only
services:
    {{ compose_dev }} up -d --wait

# Start local postgres/redis and the Mini App dev server
dev:
    {{ compose_dev }} up -d --wait
    pnpm --filter @akkiabot/webapp dev

# Stop local dev services
dev-down:
    {{ compose_dev }} --profile observability down

# --- Real Telegram acceptance in the Telegram TEST environment (docs/e2e-acceptance.md). Never run in CI.

# Store this machine's 1Password service account token (prompted, not echoed)
e2e-token-init *args:
    uv run akkiabot-e2e token-init {{ args }}

# Connect to the Telegram test environment and confirm test_mode (no account needed)
e2e-check-env:
    uv run akkiabot-e2e op-exec --env-file tools/e2e/login.env.tpl -- akkiabot-e2e check-env

# Interactive login of a test-environment account (role: admin | member); session goes to 1Password
e2e-login role *args:
    uv run akkiabot-e2e op-exec --env-file tools/e2e/login.env.tpl -- akkiabot-e2e login --role {{ role }} {{ args }}

# Store a session file from `e2e-login <role> --out <file>` into 1Password (via stdin)
e2e-session-store role *args:
    uv run akkiabot-e2e op-exec --env-file tools/e2e/login.env.tpl -- akkiabot-e2e session-store --role {{ role }} {{ args }}

# Create the test bot via the test-environment BotFather and write it back to 1Password
e2e-bootstrap-bot *args:
    uv run akkiabot-e2e op-exec --env-file tools/e2e/staging.env.tpl -- akkiabot-e2e bootstrap-bot {{ args }}

# Start the bot (test environment) from this checkout and run the live M2/M3 acceptance
e2e *args:
    uv run akkiabot-e2e op-exec --env-file tools/e2e/staging.env.tpl -- akkiabot-e2e run --serve {{ args }}

# Evaluate the antispam prompt on the offline data set (calls the real LLM; LLM_* / ANTISPAM_* from env or .env)
antispam-eval *args:
    uv run akkiabot-antispam-eval {{ args }}

# Staging release acceptance: full run against the deployed staging stack (no local bot).
# Add `--record-status <sha>` to write the GitHub commit status that production promotion requires.
e2e-staging *args:
    uv run akkiabot-e2e op-exec --env-file tools/e2e/staging-release.env.tpl -- akkiabot-e2e run {{ args }}

# Regenerate the OpenAPI document and the frontend API types (commit both)
gen-api:
    uv run akkiabot openapi --output apps/webapp/openapi.json
    pnpm --filter @akkiabot/api-types gen-api

# Build the backend image and print its version
docker-build tag="akkiabot:dev":
    docker build -f deploy/Dockerfile -t {{ tag }} .
    docker run --rm {{ tag }}

# Local Grafana (otel-lgtm) + Alloy with the AkkiaBot dashboard and alerts: http://127.0.0.1:3000
dev-observability:
    {{ compose_dev }} --profile observability up -d

# Upload dashboards and alert rules to Grafana (GRAFANA_URL / GRAFANA_SA_TOKEN; docs/observability.md)
grafana-sync *args:
    uv run python -m scripts.grafana_sync {{ args }}
