"""The tool only ever talks to Telegram's test environment; no production fallback."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from akkiabot_e2e.bot_api import check_test_bot
from akkiabot_e2e.botfather import create_test_bot
from akkiabot_e2e.errors import E2EError, SafetyError
from akkiabot_e2e.redaction import Secret
from akkiabot_e2e.telethon_transport import (
    TEST_DC_ADDRESSES,
    TEST_DC_ALLOWED_ADDRESSES,
    _session,
    ensure_test_environment,
)
from akkiabot_e2e.testing import ADMIN, FakeTelegram, add_fake_botfather

PRODUCTION_DC2 = "149.154.167.50"
PRODUCTION_DC1 = "149.154.175.53"
#: What the test servers' help.getConfig advertises for DC1 (and Telethon uses
#: after PHONE_MIGRATE); seen on 2026-11-14 when logging in a +1 test account.
TEST_DC1_ADVERTISED = "149.154.175.40"
TOKEN = Secret("123456:fake-bot-token-for-tests-xxxxxxxxxxx")


def _saved(address: str) -> Secret:
    session = StringSession()
    session.set_dc(2, address, 443)
    session.auth_key = AuthKey(b"\x01" * 256)  # save() is empty without a key
    return Secret(session.save())


def test_new_sessions_start_on_a_test_dc() -> None:
    assert _session(None).server_address in TEST_DC_ADDRESSES.values()


def test_test_session_is_accepted() -> None:
    assert _session(_saved(TEST_DC_ADDRESSES[2])).server_address == TEST_DC_ADDRESSES[2]


def test_session_on_advertised_test_dc1_is_accepted() -> None:
    assert _session(_saved(TEST_DC1_ADVERTISED)).server_address == TEST_DC1_ADVERTISED


def test_allowed_addresses_contain_no_production_dc() -> None:
    assert not {PRODUCTION_DC1, PRODUCTION_DC2} & TEST_DC_ALLOWED_ADDRESSES


@pytest.mark.parametrize(
    "value", [_saved(PRODUCTION_DC2), _saved(PRODUCTION_DC1), Secret("not-a-session")]
)
def test_production_or_invalid_sessions_are_refused(value: Secret) -> None:
    with pytest.raises(SafetyError):
        _session(value)


class FakeClient:
    def __init__(self, *, test_mode: bool, address: str) -> None:
        self.session = SimpleNamespace(server_address=address)
        self.test_mode = test_mode
        self.disconnected = False

    async def __call__(self, request: Any) -> Any:
        return SimpleNamespace(test_mode=self.test_mode)

    async def disconnect(self) -> None:
        self.disconnected = True


@pytest.mark.parametrize("address", [TEST_DC_ADDRESSES[2], TEST_DC1_ADVERTISED])
async def test_server_confirmed_test_mode_passes(address: str) -> None:
    # Includes the state after PHONE_MIGRATE to test DC1 (regression: login of
    # +1 test accounts was refused as "not the test environment").
    client = FakeClient(test_mode=True, address=address)
    await ensure_test_environment(client)
    assert not client.disconnected


@pytest.mark.parametrize(
    ("test_mode", "address"),
    [(False, TEST_DC_ADDRESSES[2]), (True, PRODUCTION_DC2)],
    ids=["server-not-test-mode", "production-address"],
)
async def test_non_test_server_disconnects_without_fallback(test_mode: bool, address: str) -> None:
    client = FakeClient(test_mode=test_mode, address=address)
    with pytest.raises(SafetyError, match="不会回退"):
        await ensure_test_environment(client)
    assert client.disconnected


def _bot_api(responses: dict[str, httpx.Response], seen: list[str]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return responses.get(request.url.path, httpx.Response(404, json={"ok": False}))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_bot_check_uses_only_the_test_endpoint() -> None:
    seen: list[str] = []
    ok = httpx.Response(200, json={"ok": True, "result": {"username": "E2E_Bot", "is_bot": True}})
    path = f"/bot{TOKEN.reveal()}/test/getMe"
    async with _bot_api({path: ok}, seen) as http:
        await check_test_bot(TOKEN, "e2e_bot", http)
        with pytest.raises(SafetyError, match="不一致"):
            await check_test_bot(TOKEN, "other_bot", http)
    assert all("/test/" in url for url in seen)


async def test_production_token_is_refused_without_trying_production() -> None:
    seen: list[str] = []
    async with _bot_api({}, seen) as http:  # test endpoint answers 404 / ok=false
        with pytest.raises(SafetyError, match="不会回退"):
            await check_test_bot(TOKEN, "e2e_bot", http)
    assert len(seen) == 1
    assert "/test/getMe" in seen[0]


def _father_world(taken: int = 0) -> FakeTelegram:
    world = FakeTelegram()
    world.add_user(ADMIN)
    add_fake_botfather(world, taken=taken, token=TOKEN.reveal())
    return world


async def test_botfather_flow_returns_username_and_token() -> None:
    world = _father_world(taken=1)
    names = iter(["first_bot", "second_bot"])
    username, token = await create_test_bot(
        world.account(ADMIN), make_username=lambda: next(names), poll=0.01
    )
    assert (username, token) == ("second_bot", TOKEN.reveal())


async def test_botfather_gives_up_after_attempts() -> None:
    world = _father_world(taken=5)
    with pytest.raises(E2EError, match="没有返回 bot token"):
        await create_test_bot(world.account(ADMIN), attempts=2, poll=0.01)
