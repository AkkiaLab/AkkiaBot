#!/usr/bin/env bash
# Fail if a production build (apps/webapp/dist) contains the dev-only Telegram
# environment mock (src/dev/mockTelegramEnv.ts). Run after `vite build`.
set -euo pipefail

dist="${1:-$(dirname "$0")/../dist}"
if [[ ! -f "$dist/index.html" ]]; then
  echo "error: $dist/index.html not found; run the production build first" >&2
  exit 2
fi

# The module's marker string, plus the SDK function only the mock calls.
if grep -rlE "akkiabot-dev-telegram-mock|mockTelegramEnv|installDevTelegramMock" "$dist"; then
  echo "error: dev-only Telegram mock found in the production build (files above)" >&2
  exit 1
fi
echo "ok: no dev-only Telegram mock in $dist"
