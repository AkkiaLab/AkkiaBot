from ipaddress import ip_address

import pytest

from akkiabot_whois import InvalidIpError, TTLCache, ensure_public, extract_ip, parse_ip
from akkiabot_whois.models import join_location


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.1.1.1", "1.1.1.1"),
        (" 8.8.8.8 ", "8.8.8.8"),
        ("2001:4860:4860::8888", "2001:4860:4860::8888"),
        ("[2001:db8::1]", "2001:db8::1"),
    ],
)
def test_parse_ip(text: str, expected: str) -> None:
    assert parse_ip(text) == ip_address(expected)


@pytest.mark.parametrize("text", ["", "abc", "1.1.1", "01.1.1.1", "1.1.1.1:53", "1.1.1.1/24"])
def test_parse_ip_rejects(text: str) -> None:
    with pytest.raises(InvalidIpError):
        parse_ip(text)


@pytest.mark.parametrize(
    "text", ["10.1.2.3", "192.168.0.1", "127.0.0.1", "100.64.0.1", "224.0.0.1", "::1", "fd00::1"]
)
def test_ensure_public_rejects(text: str) -> None:
    with pytest.raises(InvalidIpError):
        ensure_public(ip_address(text))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("连接 1.1.1.1 失败了", "1.1.1.1"),
        ("server: 8.8.8.8:53.", "8.8.8.8"),
        ("ip=9.9.9.9，谢谢", "9.9.9.9"),
        ("look at [2606:4700:4700::1111]:443", "2606:4700:4700::1111"),
        ("version 1.2.3 then 4.4.4.4", "4.4.4.4"),
        ("deadbeef cafe 2001:db8::1", "2001:db8::1"),
        ("IP:1.1.1.1", "1.1.1.1"),
        ("ip:8.8.8.8", "8.8.8.8"),
        ("IP: 1.1.1.1", "1.1.1.1"),
        ("IP:1.1.1.1:443", "1.1.1.1"),
        ("ip：9.9.9.9", "9.9.9.9"),
        ("IP:2001:db8::1", "2001:db8::1"),
        ("addr ::ffff:1.2.3.4", "::ffff:1.2.3.4"),
        ("loopback ::1", "::1"),
    ],
)
def test_extract_ip(text: str, expected: str) -> None:
    assert extract_ip(text) == ip_address(expected)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no address here",
        "1.2.3",
        "12:30 见",
        "999.1.1.1",
        "IP:999.1.1.1",
        "1.2.3.4.5",
        "ip:1.2.3",
    ],
)
def test_extract_ip_none(text: str) -> None:
    assert extract_ip(text) is None


def test_join_location() -> None:
    assert join_location("Tokyo", "Tokyo", "JP") == "Tokyo, JP"
    assert join_location("", None, "US") == "US"
    assert join_location() == ""


def test_ttl_cache_lru_and_validation() -> None:
    cache: TTLCache[str, int] = TTLCache(ttl=10, maxsize=2)
    cache.set("a", 1)
    cache.set("b", 2)
    assert cache.get("a") == 1  # "b" becomes least recently used
    cache.set("c", 3)
    assert cache.get("b") is None
    assert (cache.get("a"), cache.get("c")) == (1, 3)
    cache.clear()
    assert len(cache) == 0
    with pytest.raises(ValueError, match="ttl"):
        TTLCache(ttl=0)
    with pytest.raises(ValueError, match="maxsize"):
        TTLCache(ttl=1, maxsize=0)
