# 1Password secret references for the staging release acceptance
# (`just e2e-staging`, docs/e2e-acceptance.md "staging 发版验收"). Unlike
# staging.env.tpl this does not start a bot: it drives the deployed staging
# stack (staging bot + bot-api-staging + staging Mini App) with the e2e test
# accounts. Only 1Password references live here; values exist only in the child process.

# --- bot under test: the long-running staging bot (getMe check in the test environment)
TELEGRAM_BOT_TOKEN=op://AkkiaBot/staging-bot/bot_token
E2E_BOT_USERNAME=op://AkkiaBot/staging-bot/bot_username
TELEGRAM_TEST_ENVIRONMENT=true

# --- deployed staging endpoints (captcha Mini App chain; version.json is checked by --record-status)
E2E_API_BASE_URL=https://bot-api-staging.akkia.moe
E2E_WEBAPP_URL=https://staging.akkiabot-webapp.pages.dev

# --- user-account automation (item "e2e-telegram")
E2E_API_ID=op://AkkiaBot/e2e-telegram/api_id
E2E_API_HASH=op://AkkiaBot/e2e-telegram/api_hash
E2E_ADMIN_SESSION=op://AkkiaBot/e2e-telegram/admin_session
E2E_MEMBER_SESSION=op://AkkiaBot/e2e-telegram/member_session
E2E_ALLOWED_USER_IDS=op://AkkiaBot/e2e-telegram/allowed_user_ids
E2E_ALLOWED_CHAT_IDS=op://AkkiaBot/e2e-telegram/allowed_chat_ids
# A group of its own, so the staging bot never shares a group with the e2e bot.
E2E_CHAT_TITLE=AkkiaBot Staging 发版验收群

# --- antispam: the staging bot classifies with these (deploy/staging/app.env.tpl); the
# run only needs them to know antispam is configured and to look up Langfuse traces
LLM_API_KEY=op://AkkiaBot/staging-services/llm_api_key
LLM_MODEL=op://AkkiaBot/staging-services/llm_model
LANGFUSE_PUBLIC_KEY=op://AkkiaBot/staging-services/langfuse_public_key
LANGFUSE_SECRET_KEY=op://AkkiaBot/staging-services/langfuse_secret_key
LANGFUSE_HOST=op://AkkiaBot/staging-services/langfuse_host
