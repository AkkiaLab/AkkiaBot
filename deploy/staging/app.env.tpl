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

# --- antispam (M5): OpenAI-compatible LLM + Langfuse, shared dev/staging services
LLM_BASE_URL=op://AkkiaBot/staging-services/llm_base_url
LLM_API_KEY=op://AkkiaBot/staging-services/llm_api_key
LLM_MODEL=op://AkkiaBot/staging-services/llm_model
LANGFUSE_PUBLIC_KEY=op://AkkiaBot/staging-services/langfuse_public_key
LANGFUSE_SECRET_KEY=op://AkkiaBot/staging-services/langfuse_secret_key
LANGFUSE_HOST=op://AkkiaBot/staging-services/langfuse_host

# --- captcha / Mini App: the staging build of the webapp, published to the
# fixed Pages alias by .github/workflows/webapp-deploy.yml (docs/webapp.md)
TURNSTILE_SECRET_KEY=op://AkkiaBot/staging-turnstile/secret_key
WEBAPP_URL=https://staging.akkiabot-webapp.pages.dev
CORS_ALLOW_ORIGINS=https://staging.akkiabot-webapp.pages.dev

# --- observability
OBSERVABILITY_LOG_LEVEL=INFO
OBSERVABILITY_SENTRY_DSN=op://AkkiaBot/staging-services/sentry_dsn
OBSERVABILITY_SENTRY_ENVIRONMENT=staging
