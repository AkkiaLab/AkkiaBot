# Staging application config as 1Password secret references (vault
# AkkiaBot-Staging, same items as tools/e2e/staging.env.tpl). Render on the
# staging host into `app.env` next to compose.yaml:
#
#   umask 077 && op inject -i app.env.tpl -o app.env
#
# Only op:// paths and non-secret settings live here.

# --- telegram: the test bot, Telegram test environment (/bot<token>/test/)
TELEGRAM_BOT_TOKEN=op://AkkiaBot-Staging/runtime/bot_token
TELEGRAM_MODE=polling
TELEGRAM_TEST_ENVIRONMENT=true

# --- whois (/ip)
WHOIS_IPINFO_TOKEN=op://AkkiaBot-Staging/runtime/ipinfo_token

# --- captcha / Mini App (filled in once M6/M7 land; staging preview of the webapp)
WEBAPP_URL=
CORS_ALLOW_ORIGINS=

# --- observability
OBSERVABILITY_LOG_LEVEL=INFO
OBSERVABILITY_SENTRY_DSN=op://AkkiaBot-Staging/runtime/sentry_dsn
OBSERVABILITY_SENTRY_ENVIRONMENT=staging
