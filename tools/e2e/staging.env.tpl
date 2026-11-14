# 1Password secret references for `just e2e` (op run --env-file=...).
# Only op:// paths live here; values are resolved by `op run` at run time and
# exist only in the child process environment. Field list: docs/e2e-acceptance.md.

# --- bot under test (vault AkkiaBot-Staging, item "runtime"); test-environment bot only
TELEGRAM_BOT_TOKEN=op://AkkiaBot-Staging/runtime/bot_token
OBSERVABILITY_SENTRY_DSN=op://AkkiaBot-Staging/runtime/sentry_dsn
WHOIS_IPINFO_TOKEN=op://AkkiaBot-Staging/runtime/ipinfo_token

# --- user-account automation (item "telegram-e2e")
E2E_API_ID=op://AkkiaBot-Staging/telegram-e2e/api_id
E2E_API_HASH=op://AkkiaBot-Staging/telegram-e2e/api_hash
E2E_BOT_USERNAME=op://AkkiaBot-Staging/telegram-e2e/bot_username
E2E_ADMIN_SESSION=op://AkkiaBot-Staging/telegram-e2e/admin_session
E2E_MEMBER_SESSION=op://AkkiaBot-Staging/telegram-e2e/member_session
E2E_ALLOWED_USER_IDS=op://AkkiaBot-Staging/telegram-e2e/allowed_user_ids
E2E_ALLOWED_CHAT_IDS=op://AkkiaBot-Staging/telegram-e2e/allowed_chat_ids

# --- plain settings (optional; defaults shown)
# Bot API test endpoints (/bot<token>/test/); `run --serve` forces this anyway.
TELEGRAM_TEST_ENVIRONMENT=true
OBSERVABILITY_SENTRY_ENVIRONMENT=staging
# E2E_CHAT_TITLE=AkkiaBot E2E 自动验收群
# E2E_REPLY_TIMEOUT=30
# E2E_FLOOD_MAX_WAIT=120
# E2E_SEND_INTERVAL=2
