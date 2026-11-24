from pathlib import Path

import pytest
from pydantic import ValidationError

from akkiabot_core.config import (
    load_database_settings,
    load_observability_settings,
    load_settings,
)

# mock: 人工构造的不可用测试值，不符合真实凭据格式，不是凭据。
MOCK_BOT_TOKEN = "123456:fake-bot-token-for-tests"
# mock: 人工构造的不可用测试值，不符合真实凭据格式，不是凭据。
MOCK_SENTRY_DSN = "https://pub@o1.ingest.sentry.io/1"
PG = "postgresql+asyncpg://akkiabot:s3cr3t-pw@db:5432/akkiabot"


@pytest.fixture
def required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", MOCK_BOT_TOKEN)
    monkeypatch.setenv("DB_URL", PG)


@pytest.mark.usefixtures("required_env")
def test_loads_groups_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://:redispw@redis:6379/0")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://bot.akkia.moe, https://preview.pages.dev")
    monkeypatch.setenv("OBSERVABILITY_LOG_LEVEL", "debug")
    monkeypatch.setenv("OBSERVABILITY_SENTRY_TRACES_SAMPLE_RATE", "0.25")
    monkeypatch.setenv("WEBAPP_INIT_DATA_MAX_AGE", "600")
    monkeypatch.setenv("LLM_MODEL", "doubao-pro")
    monkeypatch.setenv("WHOIS_RATE_LIMIT", "3")

    s = load_settings()

    assert s.telegram.bot_token.get_secret_value() == MOCK_BOT_TOKEN
    assert s.db.url == PG
    assert s.redis.url is not None
    assert s.cors.allow_origins == ["https://bot.akkia.moe", "https://preview.pages.dev"]
    assert s.observability.log_level == "DEBUG"
    assert s.observability.sentry_traces_sample_rate == 0.25
    assert s.observability.sentry_dsn is None
    assert s.webapp.init_data_max_age == 600
    assert s.llm.model == "doubao-pro"
    assert s.whois.rate_limit == 3
    assert s.whois.ipinfo_token is None
    assert s.antispam.exempt_message_count == 3
    assert s.antispam.structured_output == "auto"
    assert not s.langfuse.enabled


@pytest.mark.usefixtures("required_env")
def test_antispam_and_langfuse_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTISPAM_EXEMPT_MESSAGE_COUNT", "10")
    monkeypatch.setenv("ANTISPAM_STRUCTURED_OUTPUT", "off")
    monkeypatch.setenv("ANTISPAM_PROMPT_FILE", "prompts/antispam.txt")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "fake-langfuse-public-for-tests")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "fake-langfuse-secret-for-tests")
    monkeypatch.setenv("LANGFUSE_HOST", "https://langfuse.example.test")
    s = load_settings()
    assert s.antispam.exempt_message_count == 10
    assert s.antispam.structured_output == "off"
    assert s.antispam.prompt_file == Path("prompts/antispam.txt")
    assert s.langfuse.enabled
    rendered = repr(s) + str(s.redacted())
    assert "fake-langfuse-secret-for-tests" not in rendered
    assert "fake-langfuse-public-for-tests" not in rendered


@pytest.mark.usefixtures("required_env")
def test_reads_dotenv_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DB_URL")
    (tmp_path / ".env").write_text("DB_URL=sqlite+aiosqlite:///./x.db\nLLM_MODEL=m\n")
    s = load_settings()
    assert s.db.url == "sqlite+aiosqlite:///./x.db"
    assert s.llm.model == "m"


def test_missing_required_fails_at_startup() -> None:
    with pytest.raises(ValidationError) as exc:
        load_settings()
    missing = {".".join(map(str, e["loc"])) for e in exc.value.errors()}
    assert missing == {"telegram", "db"}


@pytest.mark.usefixtures("required_env")
def test_blank_required_value_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    with pytest.raises(ValidationError) as exc:
        load_settings()
    assert [e["loc"] for e in exc.value.errors()] == [("telegram", "bot_token")]


@pytest.mark.usefixtures("required_env")
def test_blank_optional_value_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBSERVABILITY_SENTRY_DSN", "")
    monkeypatch.setenv("REDIS_URL", "")
    s = load_settings()
    assert s.observability.sentry_dsn is None
    assert s.redis.url is None


@pytest.mark.parametrize(
    ("env", "value"),
    [
        ("DB_URL", "mysql+aiomysql://u:p@h/db"),
        ("DB_URL", "postgresql://u:p@h/db"),  # sync driver
        ("DB_URL", "sqlite:///x.db"),  # sync driver
        ("DB_URL", "postgresql+aiosqlite://u:p@h/db"),  # cross pairing
        ("DB_URL", "sqlite+asyncpg:///x.db"),  # cross pairing
        ("DB_URL", "postgresql+psycopg://u:p@h/db"),
        ("OBSERVABILITY_SENTRY_TRACES_SAMPLE_RATE", "1.5"),
        ("OBSERVABILITY_LOG_LEVEL", "verbose"),
        ("WEBAPP_INIT_DATA_MAX_AGE", "0"),
        ("WHOIS_RATE_LIMIT", "0"),
        ("WHOIS_TIMEOUT", "-1"),
        ("WEBAPP_URL", "not a url"),
        ("ANTISPAM_STRUCTURED_OUTPUT", "maybe"),
        ("ANTISPAM_TIMEOUT", "0"),
        ("LANGFUSE_HOST", "not a url"),
    ],
)
@pytest.mark.usefixtures("required_env")
def test_invalid_values_rejected(monkeypatch: pytest.MonkeyPatch, env: str, value: str) -> None:
    monkeypatch.setenv(env, value)
    with pytest.raises(ValidationError):
        load_settings()


@pytest.mark.usefixtures("required_env")
def test_secrets_are_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "sk-llm-key")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "ts-secret")
    monkeypatch.setenv("WHOIS_IPINFO_TOKEN", "ipinfo-secret")
    monkeypatch.setenv("OBSERVABILITY_SENTRY_DSN", MOCK_SENTRY_DSN)
    s = load_settings()

    rendered = repr(s) + str(s) + str(s.redacted())
    for secret in (
        MOCK_BOT_TOKEN,
        "s3cr3t-pw",
        "sk-llm-key",
        "ts-secret",
        "ipinfo-secret",
        "pub@o1",
    ):
        assert secret not in rendered
    assert s.redacted()["db"]["url"] == "postgresql+asyncpg://akkiabot:***@db:5432/akkiabot"
    # ... and every one of them is handed to the Sentry scrubber.
    assert set(s.secret_values()) == {
        MOCK_BOT_TOKEN,
        "s3cr3t-pw",
        "sk-llm-key",
        "ts-secret",
        "ipinfo-secret",
        MOCK_SENTRY_DSN,
    }


@pytest.mark.usefixtures("required_env")
def test_validation_error_does_not_echo_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_URL", "mysql://root:hunter2@h/db")
    with pytest.raises(ValidationError) as exc:
        load_settings()
    assert "hunter2" not in str(exc.value)


@pytest.mark.parametrize(
    "url", ["postgresql+asyncpg://u:p@h:5432/db", "sqlite+aiosqlite:///./data/bot.db"]
)
@pytest.mark.usefixtures("required_env")
def test_supported_db_urls(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("DB_URL", url)
    assert load_settings().db.url == url


def test_partial_loaders_do_not_need_bot_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_URL", PG)
    assert load_database_settings().url == PG
    assert load_observability_settings().log_format == "json"


@pytest.mark.usefixtures("required_env")
def test_runtime_defaults() -> None:
    s = load_settings()
    assert s.telegram.mode == "polling"
    assert s.telegram.message_log_retention_days == 30
    assert s.telegram.start_payload_ttl == 600
    assert (s.server.host, s.server.port) == ("0.0.0.0", 8000)  # noqa: S104
    assert (s.observability.metrics_host, s.observability.metrics_port) == ("127.0.0.1", 9464)


@pytest.mark.usefixtures("required_env")
def test_webhook_mode_requires_url_and_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_MODE", "webhook")
    with pytest.raises(ValidationError, match="TELEGRAM_WEBHOOK_URL"):
        load_settings()
    monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", "https://bot-api.akkia.moe")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cret_webhook-token")
    s = load_settings()
    assert s.telegram.webhook_secret is not None
    assert "s3cret_webhook-token" not in str(s.redacted())


@pytest.mark.usefixtures("required_env")
def test_webhook_secret_charset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "bad secret!")
    with pytest.raises(ValidationError, match="webhook_secret"):
        load_settings()


@pytest.mark.usefixtures("required_env")
def test_foreign_server_env_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVER_SOFTWARE", "nginx")
    monkeypatch.setenv("SERVER_PORT", "9000")
    assert load_settings().server.port == 9000


@pytest.mark.usefixtures("required_env")
def test_telegram_test_environment_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    assert load_settings().telegram.test_environment is False
    monkeypatch.setenv("TELEGRAM_TEST_ENVIRONMENT", "true")
    assert load_settings().telegram.test_environment is True
