import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import pytest

from akkiabot_core.telegram import (
    InitDataError,
    escape_markdown_v2,
    escape_markdown_v2_code,
    parse_init_data,
    sign_init_data,
    verify_init_data,
)

BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
MAX_AGE = timedelta(hours=1)
USER = {"id": 987654321, "first_name": "Akkia", "username": "akkia", "language_code": "zh"}


def make_init_data(*, auth_date: datetime = NOW, token: str = BOT_TOKEN, **extra: str) -> str:
    fields = {
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(USER, separators=(",", ":")),
        "auth_date": str(int(auth_date.timestamp())),
        "start_param": "captcha_abc",
        **extra,
    }
    return urlencode({**fields, "hash": sign_init_data(fields, token)})


def test_known_vector() -> None:
    # Independent check of the algorithm (HMAC_SHA256(HMAC_SHA256("WebAppData", token), dcs)).
    import hashlib
    import hmac

    fields = {"auth_date": "1", "user": "{}"}
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, b"auth_date=1\nuser={}", hashlib.sha256).hexdigest()
    assert sign_init_data(fields, BOT_TOKEN) == expected


def test_valid_signature() -> None:
    data = verify_init_data(make_init_data(), BOT_TOKEN, max_age=MAX_AGE, now=NOW)
    assert data is not None
    assert data.user is not None
    assert data.user.id == 987654321
    assert data.user.username == "akkia"
    assert data.auth_date == NOW
    assert data.start_param == "captcha_abc"


def test_valid_with_extra_signature_field() -> None:
    # Newer clients add a `signature` field; it is part of the checked data.
    raw = make_init_data(signature="ed25519sig")
    assert verify_init_data(raw, BOT_TOKEN, max_age=MAX_AGE, now=NOW) is not None


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(make_init_data(token="654321:other-token"), id="other-bot-token"),
        pytest.param(make_init_data().replace("captcha_abc", "captcha_xyz"), id="tampered-field"),
        pytest.param(make_init_data() + "&chat_type=group", id="added-field"),
        pytest.param(make_init_data().split("&hash=")[0], id="missing-hash"),
        pytest.param(make_init_data() + "&hash=00", id="duplicate-hash"),
    ],
)
def test_bad_signature(raw: str) -> None:
    assert verify_init_data(raw, BOT_TOKEN, max_age=MAX_AGE, now=NOW) is None


def test_bad_signature_reason() -> None:
    with pytest.raises(InitDataError) as exc:
        parse_init_data(make_init_data(token="1:x"), BOT_TOKEN, max_age=MAX_AGE, now=NOW)
    assert exc.value.reason == "bad_signature"


def test_expired() -> None:
    raw = make_init_data(auth_date=NOW - MAX_AGE - timedelta(seconds=1))
    assert verify_init_data(raw, BOT_TOKEN, max_age=MAX_AGE, now=NOW) is None
    with pytest.raises(InitDataError) as exc:
        parse_init_data(raw, BOT_TOKEN, max_age=MAX_AGE, now=NOW)
    assert exc.value.reason == "expired"


def test_at_max_age_boundary_is_valid() -> None:
    raw = make_init_data(auth_date=NOW - MAX_AGE)
    assert verify_init_data(raw, BOT_TOKEN, max_age=MAX_AGE, now=NOW) is not None


def test_auth_date_far_in_future_rejected() -> None:
    raw = make_init_data(auth_date=NOW + timedelta(minutes=10))
    with pytest.raises(InitDataError) as exc:
        parse_init_data(raw, BOT_TOKEN, max_age=MAX_AGE, now=NOW)
    assert exc.value.reason == "auth_date_in_future"


@pytest.mark.parametrize("raw", ["", "%%%", "no-equals-sign"])
def test_malformed(raw: str) -> None:
    assert verify_init_data(raw, BOT_TOKEN, max_age=MAX_AGE, now=NOW) is None


def test_invalid_user_json_rejected() -> None:
    fields = {"auth_date": str(int(NOW.timestamp())), "user": "{not json"}
    raw = urlencode({**fields, "hash": sign_init_data(fields, BOT_TOKEN)})
    with pytest.raises(InitDataError) as exc:
        parse_init_data(raw, BOT_TOKEN, max_age=MAX_AGE, now=NOW)
    assert exc.value.reason == "invalid_payload"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("plain text", "plain text"),
        ("1.1.1.1", r"1\.1\.1\.1"),
        ("_*[]()~`>#+-=|{}.!", r"\_\*\[\]\(\)\~\`\>\#\+\-\=\|\{\}\.\!"),
        ("a\\b", "a\\\\b"),
        ("AS13335 Cloudflare, Inc.", r"AS13335 Cloudflare, Inc\."),
        ("中文（全角）", "中文（全角）"),
    ],
)
def test_escape_markdown_v2(text: str, expected: str) -> None:
    assert escape_markdown_v2(text) == expected


def test_escape_markdown_v2_code() -> None:
    assert escape_markdown_v2_code("a`b\\c.d_e") == "a\\`b\\\\c.d_e"
