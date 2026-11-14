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

# Run pytest against the compose postgres/redis (start them with `just dev` or `just services`)
test-integration *args:
    AKKIABOT_TEST_DATABASE_URL="postgresql+asyncpg://akkiabot:akkiabot@127.0.0.1:5432/akkiabot" \
    AKKIABOT_TEST_REDIS_URL="redis://127.0.0.1:6379/15" \
    uv run pytest {{ args }}

# Apply database migrations to DB_URL (reads .env)
migrate revision="head":
    uv run akkiabot migrate {{ revision }}

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
    {{ compose_dev }} down

# Regenerate frontend API types from the backend OpenAPI schema
gen-api:
    @echo "gen-api: 后端尚未提供 HTTP API（M2 引入 FastAPI 后接入 openapi-typescript），当前无需生成。"

# Build the backend image and print its version
docker-build tag="akkiabot:dev":
    docker build -f deploy/Dockerfile -t {{ tag }} .
    docker run --rm {{ tag }}
