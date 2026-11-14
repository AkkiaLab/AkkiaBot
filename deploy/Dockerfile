# syntax=docker/dockerfile:1.7
# Backend image (no frontend). Build from the repository root:
#   docker build -f deploy/Dockerfile -t akkiabot .

ARG PYTHON_VERSION=3.13

# ---------------------------------------------------------------- builder
FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /src

# Third-party dependencies first, so source edits keep this layer cached.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=apps/server/pyproject.toml,target=apps/server/pyproject.toml \
    --mount=type=bind,source=packages/core/pyproject.toml,target=packages/core/pyproject.toml \
    --mount=type=bind,source=packages/antispam/pyproject.toml,target=packages/antispam/pyproject.toml \
    --mount=type=bind,source=packages/whois/pyproject.toml,target=packages/whois/pyproject.toml \
    --mount=type=bind,source=tools/e2e/pyproject.toml,target=tools/e2e/pyproject.toml \
    uv sync --locked --no-dev --package akkiabot-server --no-install-workspace

COPY pyproject.toml uv.lock ./
COPY apps/server apps/server
COPY packages packages
# Only the metadata of the acceptance tool: uv needs every workspace member to resolve the lock.
COPY tools/e2e/pyproject.toml tools/e2e/pyproject.toml

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package akkiabot-server --no-editable

# ---------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

LABEL org.opencontainers.image.source="https://github.com/AkkiaLab/AkkiaBot" \
      org.opencontainers.image.description="AkkiaBot backend" \
      org.opencontainers.image.licenses="Apache-2.0"

RUN groupadd --system --gid 10001 akkiabot \
    && useradd --system --uid 10001 --gid akkiabot --no-create-home akkiabot

COPY --from=builder --chown=akkiabot:akkiabot /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER akkiabot
WORKDIR /app

ENTRYPOINT ["akkiabot"]
