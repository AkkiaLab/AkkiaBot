"""The read-only Langfuse probe (respx; no network)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from akkiabot_e2e.config import LangfuseCredentials, load_config, load_langfuse
from akkiabot_e2e.errors import CheckFailed
from akkiabot_e2e.langfuse import LangfuseProbe
from akkiabot_e2e.redaction import Secret
from akkiabot_e2e.testing import live_env

HOST = "https://langfuse.example.test"
# mock: 人工构造的不可用测试值，不是凭据。
MOCK_PUBLIC_KEY = "fake-langfuse-public-for-tests"
# mock: 人工构造的不可用测试值，不是凭据。
MOCK_SECRET_KEY = "fake-langfuse-secret-for-tests"
SINCE = datetime(2026, 11, 14, tzinfo=UTC)


def probe() -> LangfuseProbe:
    return LangfuseProbe(
        LangfuseCredentials(Secret(MOCK_PUBLIC_KEY), Secret(MOCK_SECRET_KEY), HOST)
    )


def page(inputs: list[str], *, cursor: str | None = None) -> httpx.Response:
    # v2 returns input/output as raw (JSON-encoded) strings.
    data = [
        {"name": "antispam.classify", "input": json.dumps([{"role": "user", "content": i}])}
        for i in inputs
    ]
    return httpx.Response(200, json={"data": data, "meta": {"cursor": cursor}})


@respx.mock
async def test_wait_for_markers_pages_and_counts() -> None:
    route = respx.get(f"{HOST}/api/public/v2/observations")
    route.side_effect = [
        page(["广告（E2E spam run1）"], cursor="next"),
        page(["天气（E2E ham0 run1）", "广告（E2E spam run1）"]),
    ]
    p = probe()
    counts = await p.wait_for_markers(["spam run1", "ham0 run1"], since=SINCE)
    await p.aclose()
    assert counts == {"spam run1": 2, "ham0 run1": 1}
    request = route.calls[0].request
    expected = base64.b64encode(f"{MOCK_PUBLIC_KEY}:{MOCK_SECRET_KEY}".encode()).decode()
    assert request.headers["authorization"] == f"Basic {expected}"
    assert request.url.params["name"] == "antispam.classify"
    assert request.url.params["type"] == "GENERATION"
    assert request.url.params["fromStartTime"] == SINCE.isoformat()
    assert "cursor" not in request.url.params
    assert route.calls[1].request.url.params["cursor"] == "next"
    assert MOCK_SECRET_KEY not in str(request.url)


@respx.mock
async def test_wait_for_markers_polls_until_ingested() -> None:
    respx.get(f"{HOST}/api/public/v2/observations").side_effect = [page([]), page(["x spam run1"])]
    p = probe()
    counts = await p.wait_for_markers(["spam run1"], since=SINCE, poll_interval=0)
    await p.aclose()
    assert counts == {"spam run1": 1}


@respx.mock
async def test_wait_for_markers_gives_up() -> None:
    respx.get(f"{HOST}/api/public/v2/observations").mock(return_value=page([]))
    p = probe()
    counts = await p.wait_for_markers(["spam run1"], since=SINCE, max_wait=0, poll_interval=0)
    await p.aclose()
    assert counts == {"spam run1": 0}


@respx.mock
async def test_http_error_is_a_failed_check() -> None:
    respx.get(f"{HOST}/api/public/v2/observations").mock(return_value=httpx.Response(401))
    p = probe()
    with pytest.raises(CheckFailed, match="HTTP 401"):
        await p.generations(since=SINCE)
    await p.aclose()


def test_langfuse_config() -> None:
    assert load_langfuse(live_env()) is None
    creds = load_langfuse(
        live_env(
            LANGFUSE_PUBLIC_KEY=MOCK_PUBLIC_KEY,
            LANGFUSE_SECRET_KEY=MOCK_SECRET_KEY,
            LANGFUSE_HOST="https://eu.langfuse.example.test/",
        )
    )
    assert creds is not None
    assert creds.host == "https://eu.langfuse.example.test"
    assert MOCK_SECRET_KEY not in repr(creds)
    config = load_config(live_env())
    assert config.antispam
    assert not load_config(live_env(LLM_MODEL="")).antispam
