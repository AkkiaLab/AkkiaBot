"""Entry points that reuse an existing session share the run lock and the rate-limit policy."""

from __future__ import annotations

import asyncio
import fcntl
import functools
import os
import re
from pathlib import Path

import pytest

from akkiabot_e2e import cli
from akkiabot_e2e.errors import RateLimitExceeded
from akkiabot_e2e.files import write_private_file
from akkiabot_e2e.pacing import Pacer
from akkiabot_e2e.redaction import Secret
from akkiabot_e2e.testing import (
    ADMIN,
    ADMIN_SESSION,
    FAKE_BOT_TOKEN,
    FakeAccount,
    FakeTelegram,
    add_fake_botfather,
    live_env,
)
from akkiabot_e2e.transport import Account, UserInfo


class FakeVault:
    name = "AkkiaBot-Staging"

    def __init__(self) -> None:
        self.fields: dict[str, dict[str, str]] = {}

    def set_fields(self, item: str, values) -> None:
        self.fields.setdefault(item, {}).update({k: v.value for k, v in values.items()})

    def add_ids(self, item: str, field: str, ids) -> list[int]:
        return list(ids)


class Harness:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.world = FakeTelegram()
        self.world.add_user(ADMIN)
        add_fake_botfather(self.world)
        self.connected: list[str] = []
        self.accounts: list[FakeAccount] = []
        self.vault = FakeVault()
        self.slept: list[float] = []
        self.lock_path = tmp_path / "state" / "akkiabot-e2e" / "run.lock"

        async def connect(role: str, session: Secret) -> Account:
            self.connected.append(role)
            account = self.world.account(ADMIN)
            self.accounts.append(account)
            return account

        async def no_check(token, username, http) -> None:
            return None

        async def sleep(seconds: float) -> None:
            self.slept.append(seconds)

        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        monkeypatch.setattr(cli, "LOCK_POLL", 0.01)
        monkeypatch.setattr(cli, "_telethon_connect", lambda api: connect)
        monkeypatch.setattr(cli, "_vault", lambda: self.vault)
        monkeypatch.setattr(cli, "Pacer", functools.partial(Pacer, sleep=sleep))
        monkeypatch.setattr("akkiabot_e2e.bot_api.check_test_bot", no_check)

    def hold_lock(self) -> int:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd


def bootstrap_env(**overrides: str) -> dict[str, str]:
    env = live_env(**overrides)
    env.pop("E2E_BOT_USERNAME")
    return env


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(tmp_path, monkeypatch)


async def test_bootstrap_does_not_connect_while_lock_is_held(harness: Harness) -> None:
    fd = harness.hold_lock()
    task = asyncio.create_task(cli._bootstrap_bot(bootstrap_env(), force=False))
    await asyncio.sleep(0.2)
    assert harness.connected == []
    assert not task.done()

    os.close(fd)  # release
    message = await asyncio.wait_for(task, 5)

    assert harness.connected == ["admin"]
    assert harness.accounts[0].closed
    assert re.search(r"@akkiabot_e2e_[0-9a-f]{6}_bot", message)
    assert harness.vault.fields["runtime"] == {"bot_token": FAKE_BOT_TOKEN}
    assert FAKE_BOT_TOKEN not in message


async def test_bootstrap_honours_short_flood_wait(harness: Harness) -> None:
    harness.world.floods["send"] = [5]
    await cli._bootstrap_bot(bootstrap_env(E2E_FLOOD_MAX_WAIT="30"), force=False)
    assert 6 in harness.slept  # waited flood seconds + 1, then retried
    assert harness.vault.fields["telegram-e2e"]["bot_username"].endswith("_bot")


async def test_bootstrap_stops_on_long_flood_wait_and_writes_nothing(harness: Harness) -> None:
    harness.world.floods["history"] = [3600]
    with pytest.raises(RateLimitExceeded, match="3600"):
        await cli._bootstrap_bot(bootstrap_env(E2E_FLOOD_MAX_WAIT="30"), force=False)
    assert harness.vault.fields == {}
    assert harness.accounts[0].closed
    assert 3601 not in harness.slept


async def test_bootstrap_spaces_botfather_messages(harness: Harness) -> None:
    await cli._bootstrap_bot(bootstrap_env(E2E_SEND_INTERVAL="5"), force=False)
    # /newbot, name, username: the 2nd and 3rd sends wait for the interval.
    assert len([s for s in harness.slept if s > 4]) == 2


async def test_session_store_waits_for_lock_before_connecting(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_file = tmp_path / "admin.session"
    write_private_file(session_file, ADMIN_SESSION + "\n")
    calls: list[str] = []

    async def session_user(api, session) -> UserInfo:
        calls.append(session.reveal())
        return ADMIN

    monkeypatch.setattr("akkiabot_e2e.telethon_transport.session_user", session_user)
    fd = harness.hold_lock()
    args = ["session-store", "--role", "admin", "--session-file", str(session_file)]
    job = asyncio.create_task(asyncio.to_thread(cli.main, args, live_env()))
    await asyncio.sleep(0.2)
    assert calls == []

    os.close(fd)
    assert await asyncio.wait_for(job, 5) == cli.EXIT_PASSED
    assert calls == [ADMIN_SESSION]
    assert not session_file.exists()
