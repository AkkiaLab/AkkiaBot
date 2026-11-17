# 1Password secret references for `just e2e` (op run --env-file=...).
# Only op:// paths live here; values are resolved by `op run` at run time and
# exist only in the child process environment. Field list: docs/e2e-acceptance.md.

# --- bot under test (vault AkkiaBot, item "e2e-bot"; shared dev/staging services in "staging-services"); test-environment bot only
TELEGRAM_BOT_TOKEN=op://AkkiaBot/e2e-bot/bot_token
OBSERVABILITY_SENTRY_DSN=op://AkkiaBot/staging-services/sentry_dsn
WHOIS_IPINFO_TOKEN=op://AkkiaBot/staging-services/ipinfo_token
# M5 antispam: OpenAI-compatible LLM + Langfuse (also read by the M5 Langfuse check)
LLM_BASE_URL=op://AkkiaBot/staging-services/llm_base_url
LLM_API_KEY=op://AkkiaBot/staging-services/llm_api_key
LLM_MODEL=op://AkkiaBot/staging-services/llm_model
LANGFUSE_PUBLIC_KEY=op://AkkiaBot/staging-services/langfuse_public_key
LANGFUSE_SECRET_KEY=op://AkkiaBot/staging-services/langfuse_secret_key
LANGFUSE_HOST=op://AkkiaBot/staging-services/langfuse_host

# --- user-account automation (item "e2e-telegram")
E2E_API_ID=op://AkkiaBot/e2e-telegram/api_id
E2E_API_HASH=op://AkkiaBot/e2e-telegram/api_hash
E2E_BOT_USERNAME=op://AkkiaBot/e2e-bot/bot_username
E2E_ADMIN_SESSION=op://AkkiaBot/e2e-telegram/admin_session
E2E_MEMBER_SESSION=op://AkkiaBot/e2e-telegram/member_session
E2E_ALLOWED_USER_IDS=op://AkkiaBot/e2e-telegram/allowed_user_ids
E2E_ALLOWED_CHAT_IDS=op://AkkiaBot/e2e-telegram/allowed_chat_ids

# --- plain settings (optional; defaults shown)
# Bot API test endpoints (/bot<token>/test/); `run --serve` forces this anyway.
TELEGRAM_TEST_ENVIRONMENT=true
# `run --serve` forces Sentry environment `e2e` so local runs never mix into staging issues.
OBSERVABILITY_SENTRY_ENVIRONMENT=e2e
# E2E_CHAT_TITLE=AkkiaBot E2E 自动验收群
# E2E_REPLY_TIMEOUT=30
# E2E_FLOOD_MAX_WAIT=120
# E2E_SEND_INTERVAL=2
# Seconds M5 waits to be sure a message is NOT deleted (switch off / exempt member)
# E2E_ANTISPAM_WAIT=20
# Seconds m5.spam waits for the bot to act on the ad: the bot's ANTISPAM_TIMEOUT
# (45) plus Telegram calls; raise it together with ANTISPAM_TIMEOUT
# E2E_ANTISPAM_TIMEOUT=60
# The bot's ANTISPAM_PURGE_DELAY (history purge after the mute)
# E2E_ANTISPAM_PURGE_DELAY=5
