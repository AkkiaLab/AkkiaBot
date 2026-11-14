# Staging application config as 1Password secret references (vault
# AkkiaBot, items staging-bot / staging-services; the bot has its own item so it never competes with
# `just e2e --serve` for updates). Render on the
# staging host into `app.env` next to compose.yaml:
#
#   umask 077 && op inject -i app.env.tpl -o app.env
#
# Only 1Password secret references and non-secret settings live here; keep
# the reference prefix out of comments (op inject resolves it everywhere).

# --- telegram: the staging bot (item "staging", not the e2e bot in "runtime"),
# Telegram test environment (/bot<token>/test/)
TELEGRAM_BOT_TOKEN=op://AkkiaBot/staging-bot/bot_token
TELEGRAM_MODE=polling
TELEGRAM_TEST_ENVIRONMENT=true

# --- whois (/ip)
WHOIS_IPINFO_TOKEN=op://AkkiaBot/staging-services/ipinfo_token

# --- captcha / Mini App (filled in once M6/M7 land; staging preview of the webapp)
WEBAPP_URL=
CORS_ALLOW_ORIGINS=

# --- observability
OBSERVABILITY_LOG_LEVEL=INFO
OBSERVABILITY_SENTRY_DSN=op://AkkiaBot/staging-services/sentry_dsn
OBSERVABILITY_SENTRY_ENVIRONMENT=staging
