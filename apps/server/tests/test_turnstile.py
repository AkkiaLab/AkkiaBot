"""Turnstile ``siteverify`` client (HTTP mocked with respx)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from akkiabot_server.services.turnstile import HttpTurnstileVerifier, TurnstileResult

URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
# mock: 人工构造的不可用测试值，不符合真实凭据格式，不是凭据。
MOCK_TURNSTILE_SECRET = "fake-turnstile-secret-for-tests"


@pytest.fixture
async def verifier() -> AsyncIterator[HttpTurnstileVerifier]:
    async with httpx.AsyncClient() as http:
        yield HttpTurnstileVerifier(
            http, secret_key=MOCK_TURNSTILE_SECRET, verify_url=URL, timeout=3.0
        )


@respx.mock
async def test_success_sends_secret_and_token(verifier: HttpTurnstileVerifier) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"success": True}))
    assert await verifier.verify("tok") == TurnstileResult(True)
    request = route.calls.last.request
    assert parse_qs(request.content.decode()) == {
        "secret": [MOCK_TURNSTILE_SECRET],
        "response": ["tok"],
    }
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"


@respx.mock
async def test_rejection_keeps_error_codes(verifier: HttpTurnstileVerifier) -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(
            200, json={"success": False, "error-codes": ["timeout-or-duplicate"]}
        )
    )
    assert await verifier.verify("tok") == TurnstileResult(False, ("timeout-or-duplicate",))


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(500), "request_failed"),
        (httpx.Response(200, text="<html>"), "invalid_response"),
        (httpx.Response(200, json={"error-codes": []}), "invalid_response"),
    ],
)
async def test_bad_answers_are_failures(
    verifier: HttpTurnstileVerifier, response: httpx.Response, code: str
) -> None:
    with respx.mock:
        respx.post(URL).mock(return_value=response)
        assert await verifier.verify("tok") == TurnstileResult(False, (code,))


@respx.mock
async def test_network_error_is_a_failure(
    verifier: HttpTurnstileVerifier, caplog: pytest.LogCaptureFixture
) -> None:
    respx.post(URL).mock(side_effect=httpx.ConnectError("boom"))
    assert await verifier.verify("tok") == TurnstileResult(False, ("request_failed",))
    assert MOCK_TURNSTILE_SECRET not in caplog.text
