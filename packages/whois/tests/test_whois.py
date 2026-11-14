from collections.abc import AsyncIterator
from ipaddress import ip_address

import httpx
import pytest
import respx

from akkiabot_whois import (
    InvalidIpError,
    IpApiProvider,
    IpInfo,
    IpinfoProvider,
    TTLCache,
    WhoisService,
)

TOKEN = "test-ipinfo-token"
IPINFO_URL = "https://ipinfo.io/1.1.1.1"
IPAPI_URL = "http://ip-api.com/json/1.1.1.1"

IPINFO_OK = {
    "ip": "1.1.1.1",
    "hostname": "one.one.one.one",
    "city": "Brisbane",
    "region": "Queensland",
    "country": "AU",
    "org": "AS13335 Cloudflare, Inc.",
    "anycast": True,
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


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def __call__(self, provider: str, result: str) -> None:
        self.events.append((provider, result))


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(timeout=1) as c:
        yield c


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def service(client: httpx.AsyncClient, recorder: Recorder, clock: FakeClock) -> WhoisService:
    return WhoisService(
        [IpinfoProvider(client, TOKEN), IpApiProvider(client)],
        TTLCache(ttl=60, clock=clock),
        observer=recorder,
    )


async def test_first_provider_answers(service: WhoisService, recorder: Recorder) -> None:
    with respx.mock(assert_all_called=False) as router:
        ipinfo = router.get(IPINFO_URL).respond(json=IPINFO_OK)
        ipapi = router.get(IPAPI_URL).respond(json=IPAPI_OK)
        info = await service.lookup("1.1.1.1")

    assert info == IpInfo(
        ip="1.1.1.1",
        location="Brisbane, Queensland, AU",
        asn="AS13335 Cloudflare, Inc.",
        org="Cloudflare, Inc.",
        isp="Cloudflare, Inc.",
        provider="ipinfo",
    )
    assert ipinfo.call_count == 1
    assert not ipapi.called
    # The token travels in a header, never in the URL (URLs end up in logs).
    request = ipinfo.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(request.url)
    assert recorder.events == [("ipinfo", "ok")]


@respx.mock
async def test_ipinfo_paid_fields(client: httpx.AsyncClient) -> None:
    respx.get(IPINFO_URL).respond(
        json=IPINFO_OK
        | {
            "asn": {"asn": "AS13335", "name": "Cloudflare, Inc."},
            "company": {"name": "APNIC and Cloudflare DNS Resolver project"},
        }
    )
    info = await IpinfoProvider(client, TOKEN).lookup(ip_address("1.1.1.1"))
    assert info.asn == "AS13335 Cloudflare, Inc."
    assert info.org == "APNIC and Cloudflare DNS Resolver project"
    assert info.isp == "Cloudflare, Inc."


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(429, json={"error": "rate limit"}),
        httpx.Response(500, text="oops"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"unexpected": True}),
        httpx.Response(200, json={"ip": "1.1.1.1", "bogon": True}),
        httpx.ConnectTimeout("timeout"),
    ],
    ids=["429", "500", "not-json", "bad-shape", "bogon", "timeout"],
)
@respx.mock
async def test_falls_back_when_first_fails(
    service: WhoisService, recorder: Recorder, failure: httpx.Response | Exception
) -> None:
    route = respx.get(IPINFO_URL)
    if isinstance(failure, Exception):
        route.side_effect = failure
    else:
        route.return_value = failure
    respx.get(IPAPI_URL).respond(json=IPAPI_OK)

    info = await service.lookup("1.1.1.1")

    assert info is not None
    assert info.provider == "ip-api"
    assert info.location == "South Brisbane, Queensland, Australia"
    assert info.asn == "AS13335 Cloudflare, Inc."
    assert info.org == "APNIC and Cloudflare DNS Resolver project"
    assert info.isp == "Cloudflare, Inc"
    assert recorder.events == [("ipinfo", "error"), ("ip-api", "ok")]


@respx.mock
async def test_ip_api_query_parameters(client: httpx.AsyncClient) -> None:
    route = respx.get(IPAPI_URL).respond(json=IPAPI_OK)
    await IpApiProvider(client).lookup(ip_address("1.1.1.1"))
    params = route.calls.last.request.url.params
    assert params["fields"] == IpApiProvider.fields
    assert params["lang"] == "en"


@respx.mock
async def test_all_providers_fail(service: WhoisService, recorder: Recorder) -> None:
    respx.get(IPINFO_URL).respond(503)
    respx.get(IPAPI_URL).respond(json={"status": "fail", "message": "quota exceeded"})

    assert await service.lookup("1.1.1.1") is None
    assert recorder.events == [("ipinfo", "error"), ("ip-api", "error"), ("none", "exhausted")]
    assert service.cache is not None
    assert len(service.cache) == 0  # failures are not cached


@respx.mock
async def test_cache_hit_and_expiry(
    service: WhoisService, recorder: Recorder, clock: FakeClock
) -> None:
    ipinfo = respx.get(IPINFO_URL).respond(json=IPINFO_OK)

    first = await service.lookup("1.1.1.1")
    second = await service.lookup(ip_address("1.1.1.1"))
    assert first == second
    assert ipinfo.call_count == 1
    assert recorder.events == [("ipinfo", "ok"), ("cache", "hit")]

    clock.now += 61
    await service.lookup("1.1.1.1")
    assert ipinfo.call_count == 2


@pytest.mark.parametrize(
    "value", ["", "1.1.1", "1.1.1.256", "example.com", "10.0.0.1", "127.0.0.1", "::1", "fe80::1"]
)
@respx.mock
async def test_invalid_input_never_hits_network(service: WhoisService, value: str) -> None:
    with pytest.raises(InvalidIpError):
        await service.lookup(value)
    assert not respx.calls


@respx.mock
async def test_ipv6_lookup(client: httpx.AsyncClient) -> None:
    respx.get("http://ip-api.com/json/2606:4700:4700::1111").respond(
        json=IPAPI_OK | {"query": "2606:4700:4700::1111"}
    )
    service = WhoisService([IpApiProvider(client)])
    info = await service.lookup("[2606:4700:4700::1111]")
    assert info is not None
    assert info.ip == "2606:4700:4700::1111"


def test_service_needs_providers() -> None:
    with pytest.raises(ValueError, match="provider"):
        WhoisService([])


def test_ipinfo_needs_token() -> None:
    with pytest.raises(ValueError, match="token"):
        IpinfoProvider(httpx.AsyncClient(), "")
