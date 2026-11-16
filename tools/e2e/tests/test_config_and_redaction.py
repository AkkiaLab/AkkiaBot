from __future__ import annotations

import pytest

from akkiabot_e2e.config import REQUIRED, build_redactor, load_config, missing_variables
from akkiabot_e2e.errors import ConfigError
from akkiabot_e2e.redaction import REDACTED, Redactor, Secret
from akkiabot_e2e.testing import MOCK_ADMIN_SESSION, live_env
from akkiabot_e2e.transport import Entity, entity_text


def test_missing_variables_lists_names_only() -> None:
    assert missing_variables({}) == list(REQUIRED)
    assert missing_variables(live_env(E2E_MEMBER_SESSION="  ")) == ["E2E_MEMBER_SESSION"]


def test_load_config_parses_values() -> None:
    config = load_config(live_env(E2E_ALLOWED_CHAT_IDS="-1001, -1002", E2E_CHAT_TITLE=""))
    assert config.bot_username == "akkiabot_staging_bot"
    assert config.allowed_chat_ids == {-1001, -1002}
    assert config.chat_title == "AkkiaBot E2E 自动验收群"
    assert MOCK_ADMIN_SESSION not in repr(config)


def test_antispam_waits_default_to_the_bot_budget() -> None:
    env = live_env()
    del env["E2E_ANTISPAM_TIMEOUT"], env["E2E_ANTISPAM_PURGE_DELAY"]
    config = load_config(env)
    # The bot's ANTISPAM_TIMEOUT (45) plus margin, and its ANTISPAM_PURGE_DELAY.
    assert (config.antispam_timeout, config.antispam_purge_delay) == (60.0, 5.0)
    config = load_config(live_env(E2E_ANTISPAM_TIMEOUT="90", E2E_ANTISPAM_PURGE_DELAY="8"))
    assert (config.antispam_timeout, config.antispam_purge_delay) == (90.0, 8.0)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"E2E_ALLOWED_USER_IDS": "1001"}, "两个测试账号"),
        ({"E2E_ALLOWED_USER_IDS": "1001,abc"}, "逗号分隔"),
        ({"E2E_API_ID": "x"}, "整数"),
        ({"E2E_REPLY_TIMEOUT": "-1"}, "大于 0"),
    ],
)
def test_load_config_rejects_bad_values(override: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(live_env(**override))


def test_secret_never_prints_value() -> None:
    secret = Secret("super-secret-value")
    assert "super" not in repr(secret)
    assert "super" not in f"{secret}"
    assert secret.reveal() == "super-secret-value"


def test_redactor_replaces_registered_values_only() -> None:
    redact = Redactor(["token-123456", Secret("session-abcdef"), None, "12"])
    text = redact("a token-123456 b session-abcdef c 12")
    assert text == f"a {REDACTED} b {REDACTED} c 12"


# mock: 人工构造的不可用测试值，不符合真实凭据格式，不是凭据。
MOCK_SHORT_BOT_TOKEN = "123:bot-token-value"
# mock: 人工构造的不可用测试值，不符合真实凭据格式，不是凭据。
MOCK_OP_TOKEN = "ops_abcdefgh"


def test_build_redactor_covers_sessions_and_business_secrets() -> None:
    env = live_env(TELEGRAM_BOT_TOKEN=MOCK_SHORT_BOT_TOKEN, OP_SERVICE_ACCOUNT_TOKEN=MOCK_OP_TOKEN)
    redact = build_redactor(env)
    text = redact(
        f"{MOCK_ADMIN_SESSION} {MOCK_SHORT_BOT_TOKEN} {MOCK_OP_TOKEN} {env['E2E_API_HASH']}"
    )
    assert text.split() == [REDACTED] * 4


def test_entity_text_uses_utf16_offsets() -> None:
    text = "😀 群 `-100123`"
    # The emoji takes two UTF-16 code units.
    assert entity_text(text, Entity("code", 5, 9)) == "`-100123`"
