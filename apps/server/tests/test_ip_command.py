from collections.abc import Iterator

import pytest
import respx

from akkiabot_core.config import Settings
from akkiabot_server.bot.commands.ip import format_ip_info
from akkiabot_server.runtime import Runtime
from akkiabot_server.services.ip_lookup import IpQueryStatus
from akkiabot_server.services.rate_limit import RateLimiter
from akkiabot_server.testing import FakeBotApi, UpdateFactory, metric_value
from akkiabot_whois import IpInfo

from .conftest import MakeSettings

IPINFO_URL = "https://ipinfo.io/1.1.1.1"
IPAPI_URL = "http://ip-api.com/json/1.1.1.1"
IPINFO_OK = {
    "ip": "1.1.1.1",
    "city": "Brisbane",
    "region": "Queensland",
    "country": "AU",
    "org": "AS13335 Cloudflare, Inc.",
}
IPAPI_OK = {
    "status": "success",
    "query": "1.1.1.1",
    "country": "Australia",
    "regionName": "Queensland",
    "city": "South Brisbane",
    "isp": "Cloudflare, Inc",
    "org": "APNIC and Cloudflare DNS Resolver project",
    "as": "AS13335 Cloudflare, Inc.",
}


@pytest.fixture
def settings(make_settings: MakeSettings) -> Settings:
    return make_settings(whois={"ipinfo_token": "ipinfo-test-token", "rate_limit": 2})


@pytest.fixture
def mock_api() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


def lookups(provider: str, result: str) -> float:
    return metric_value("akkiabot_whois_lookup_total", provider=provider, result=result)


def commands(result: str) -> float:
    return metric_value("akkiabot_command_total", command="ip", result=result)


async def test_ip_lookup_reply(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory, mock_api: respx.MockRouter
) -> None:
    mock_api.get(IPINFO_URL).respond(json=IPINFO_OK)
    ok_before = lookups("ipinfo", "ok")

    update = updates.command("/ip", "1.1.1.1")
    await runtime.application.process_update(update)

    (sent,) = api.calls_to("sendMessage")
    assert sent["parse_mode"] == "MarkdownV2"
    assert sent["reply_parameters"]["message_id"] == update.effective_message.message_id  # type: ignore[union-attr]
    assert sent["text"] == (
        "IP: `1.1.1.1`\n"
        "Location: `Brisbane, Queensland, AU`\n"
        "AS: `AS13335 Cloudflare, Inc.`\n"
        "Org: `Cloudflare, Inc.`\n"
        "ISP: `Cloudflare, Inc.`"
    )
    assert lookups("ipinfo", "ok") == ok_before + 1


async def test_falls_back_to_ip_api_and_records_metrics(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory, mock_api: respx.MockRouter
) -> None:
    mock_api.get(IPINFO_URL).respond(429)
    mock_api.get(IPAPI_URL).respond(json=IPAPI_OK)
    before = (lookups("ipinfo", "error"), lookups("ip-api", "ok"), lookups("cache", "hit"))

    await runtime.application.process_update(updates.command("/ip", "1.1.1.1"))
    await runtime.application.process_update(updates.command("/ip", "1.1.1.1"))

    first, second = api.sent_texts()
    assert first == second
    assert "Location: `South Brisbane, Queensland, Australia`" in first
    assert "ISP: `Cloudflare, Inc`" in first
    after = (lookups("ipinfo", "error"), lookups("ip-api", "ok"), lookups("cache", "hit"))
    assert after == (before[0] + 1, before[1] + 1, before[2] + 1)


async def test_all_providers_fail(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory, mock_api: respx.MockRouter
) -> None:
    mock_api.get(IPINFO_URL).respond(500)
    mock_api.get(IPAPI_URL).respond(json={"status": "fail", "message": "quota"})
    before = (lookups("none", "exhausted"), commands("failed"))

    await runtime.application.process_update(updates.command("/ip", "1.1.1.1"))

    assert api.sent_texts() == ["查询失败，请稍后再试。"]
    assert (lookups("none", "exhausted"), commands("failed")) == (before[0] + 1, before[1] + 1)


async def test_ip_from_replied_message(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory, mock_api: respx.MockRouter
) -> None:
    route = mock_api.get(IPINFO_URL).respond(json=IPINFO_OK)
    group = updates.group()
    replied = updates.message_dict(
        chat=group, from_user=updates.user(7, "Bob"), text="我的出口是 1.1.1.1:443，好慢"
    )
    await runtime.application.process_update(updates.command("/ip", chat=group, reply_to=replied))

    assert route.call_count == 1
    assert api.sent_texts()[0].startswith("IP: `1.1.1.1`")


@pytest.mark.parametrize("text", ["IP:1.1.1.1", "ip:1.1.1.1:443", "出口 IP：1.1.1.1。"])
async def test_ip_from_replied_labelled_text(
    runtime: Runtime,
    api: FakeBotApi,
    updates: UpdateFactory,
    mock_api: respx.MockRouter,
    text: str,
) -> None:
    route = mock_api.get(IPINFO_URL).respond(json=IPINFO_OK)
    group = updates.group()
    replied = updates.message_dict(chat=group, from_user=updates.user(7, "Bob"), text=text)
    await runtime.application.process_update(updates.command("/ip", chat=group, reply_to=replied))

    assert route.call_count == 1
    assert api.sent_texts()[0].startswith("IP: `1.1.1.1`")


async def test_ip_from_replied_caption(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory, mock_api: respx.MockRouter
) -> None:
    mock_api.get(IPINFO_URL).respond(json=IPINFO_OK)
    group = updates.group()
    replied = updates.message_dict(
        chat=group,
        from_user=updates.user(7, "Bob"),
        caption="截图里的 IP: 1.1.1.1",
        photo=[{"file_id": "f", "file_unique_id": "u", "width": 1, "height": 1}],
    )
    await runtime.application.process_update(updates.command("/ip", chat=group, reply_to=replied))
    assert api.sent_texts()[0].startswith("IP: `1.1.1.1`")


@pytest.mark.parametrize(
    ("args", "reply_text", "expected", "result"),
    [
        ((), None, "用法：/ip <IP 地址>", "usage"),
        (("1.1.1",), None, "IP 地址格式错误。", "invalid_ip"),
        (("example.com",), None, "IP 地址格式错误。", "invalid_ip"),
        (("192.168.1.1",), None, "192.168.1.1 不是公网地址，无法查询。", "not_public"),
        (("::1",), None, "::1 不是公网地址，无法查询。", "not_public"),
        ((), "没有地址", "被回复的消息中没有找到 IP 地址。", "no_ip"),
    ],
)
async def test_invalid_input(
    runtime: Runtime,
    api: FakeBotApi,
    updates: UpdateFactory,
    mock_api: respx.MockRouter,
    args: tuple[str, ...],
    reply_text: str | None,
    expected: str,
    result: str,
) -> None:
    group = updates.group()
    replied = (
        updates.message_dict(chat=group, from_user=updates.user(7, "Bob"), text=reply_text)
        if reply_text
        else None
    )
    before = commands(result)

    await runtime.application.process_update(
        updates.command("/ip", *args, chat=group, reply_to=replied)
    )

    (text,) = api.sent_texts()
    assert text.startswith(expected)
    assert "parse_mode" not in api.calls_to("sendMessage")[0]
    assert not mock_api.calls
    assert commands(result) == before + 1


async def test_rate_limited_per_user(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory, mock_api: respx.MockRouter
) -> None:
    mock_api.get(IPINFO_URL).respond(json=IPINFO_OK)
    alice, bob = updates.user(1, "Alice"), updates.user(2, "Bob")
    before = commands("rate_limited")

    for _ in range(3):
        await runtime.application.process_update(updates.command("/ip", "1.1.1.1", from_user=alice))
    # Invalid input does not consume the quota, and other users are unaffected.
    await runtime.application.process_update(updates.command("/ip", "bad", from_user=bob))
    await runtime.application.process_update(updates.command("/ip", "1.1.1.1", from_user=bob))

    texts = api.sent_texts()
    assert texts[0].startswith("IP: ")
    assert texts[1].startswith("IP: ")
    assert texts[2] == "查询太频繁，请 60 秒后再试。"
    assert texts[4].startswith("IP: ")
    assert commands("rate_limited") == before + 1


def test_markdown_v2_escaping() -> None:
    info = IpInfo(
        ip="2001:db8::1",
        location="",
        asn="AS1 `weird` \\ name_*[]",
        org="A.B (C) - D!",
        isp="",
        provider="test",
    )
    assert format_ip_info(info) == (
        "IP: `2001:db8::1`\n"
        "Location: `未知`\n"
        "AS: `AS1 \\`weird\\` \\\\ name_*[]`\n"
        "Org: `A.B (C) - D!`\n"
        "ISP: `未知`"
    )


def test_rate_limiter_window_and_sweep() -> None:
    now = [0.0]
    limiter = RateLimiter(2, 10, clock=lambda: now[0])
    assert limiter.acquire("a") == 0
    now[0] = 4
    assert limiter.acquire("a") == 0
    assert limiter.acquire("a") == pytest.approx(6)
    now[0] = 10
    assert limiter.acquire("a") == 0  # the first hit left the window
    assert limiter.acquire("b") == 0
    now[0] = 25
    limiter.acquire("c")  # sweeps idle keys
    assert len(limiter) == 1
    with pytest.raises(ValueError, match="limit"):
        RateLimiter(0, 1)


def test_status_values_are_metric_labels() -> None:
    assert {s.value for s in IpQueryStatus} == {
        "ok",
        "usage",
        "invalid_ip",
        "no_ip",
        "not_public",
        "rate_limited",
        "failed",
    }


async def test_without_ipinfo_token_uses_ip_api_only(
    make_settings: MakeSettings,
    api: FakeBotApi,
    mock_api: respx.MockRouter,
) -> None:
    mock_api.get(IPAPI_URL).respond(json=IPAPI_OK)
    rt = Runtime.build(make_settings(), request=api, get_updates_request=api)
    try:
        assert [p.name for p in rt.deps.ip_lookup.whois.providers] == ["ip-api"]
        result = await rt.deps.ip_lookup.query(1, "1.1.1.1", None)
        assert result.status is IpQueryStatus.OK
    finally:
        await rt.http.aclose()
        await rt.engine.dispose()
