"""``prune-groups``: weekly clean-up of old staging release acceptance groups (BOT-30)."""

from __future__ import annotations

import asyncio
import functools
import re
from pathlib import Path

import pytest

from akkiabot_e2e import cli
from akkiabot_e2e.config import DEFAULT_CHAT_TITLE
from akkiabot_e2e.errors import E2EError, FloodWait
from akkiabot_e2e.pacing import Pacer
from akkiabot_e2e.prune import (
    KEEP,
    STAGING_RELEASE_CHAT_TITLE,
    PruneResult,
    plan,
    prune_groups,
)
from akkiabot_e2e.redaction import Secret
from akkiabot_e2e.testing import ADMIN, MEMBER, FakeAccount, FakeTelegram, live_env
from akkiabot_e2e.transport import ChatRef

ENV_TPL = Path(__file__).resolve().parents[1] / "staging-release.env.tpl"


def run_title(stamp: str, suffix: str = "abc123") -> str:
    return f"{STAGING_RELEASE_CHAT_TITLE} {stamp}-{suffix}"


async def _no_sleep(_: float) -> None:
    return None


class FakeAllowlist:
    def __init__(self, ids: set[int]) -> None:
        self.ids = set(ids)
        self.removed: list[set[int]] = []
        self.fail = False

    def chat_ids(self) -> set[int]:
        return set(self.ids)

    def remove(self, chat_ids: set[int]) -> None:
        if self.fail:
            raise E2EError("回读 AkkiaBot/e2e-telegram/allowed_chat_ids 的值与写入不一致")
        self.removed.append(set(chat_ids))
        self.ids -= chat_ids


class World:
    """Admin-owned groups: 4 in-scope runs, plus everything that must never be deleted."""

    def __init__(self) -> None:
        self.tg = FakeTelegram()
        self.tg.add_user(ADMIN)
        self.tg.add_user(MEMBER)
        add = self.tg.add_chat
        # In scope (allowlisted), created out of order on purpose.
        self.r2 = add(run_title("20261102T010000Z"), ADMIN)
        self.r4 = add(run_title("20261114T220000Z"), ADMIN)
        self.r1 = add(run_title("20261101T010000Z"), ADMIN)
        self.r3 = add(run_title("20261110T010000Z"), ADMIN)
        # Never deleted:
        self.resident = add(DEFAULT_CHAT_TITLE, ADMIN)
        self.base_title = add(STAGING_RELEASE_CHAT_TITLE, ADMIN)  # no run id
        self.odd = add(f"{STAGING_RELEASE_CHAT_TITLE} manual-copy", ADMIN)
        self.outside = add(run_title("20261020T010000Z", "dead00"), ADMIN)  # not allowlisted
        self.foreign = add(run_title("20261001T010000Z", "f00000"), MEMBER)  # not created by admin
        self.allowlist = FakeAllowlist(
            {
                c.id
                for c in (
                    self.r1,
                    self.r2,
                    self.r3,
                    self.r4,
                    self.resident,
                    self.base_title,
                    self.odd,
                    self.foreign,
                )
            }
        )
        self.admin = self.tg.account(ADMIN)

    async def prune(self, *, apply: bool) -> PruneResult:
        pacer = Pacer(send_interval=0, sleep=_no_sleep)
        return await prune_groups(self.admin, self.allowlist, pacer, apply=apply)


def test_plan_scope_and_keep_newest() -> None:
    groups = [
        ChatRef(-1, run_title("20261101T010000Z")),
        ChatRef(-2, run_title("20261114T010000Z")),
        ChatRef(-3, run_title("20261108T010000Z")),
        ChatRef(-4, DEFAULT_CHAT_TITLE),
        ChatRef(-5, run_title("20261115T010000Z", "ABCDEF")),  # upper-case hex: not a run id
        ChatRef(-6, run_title("20261116T010000Z") + " (copy)"),
        ChatRef(-7, run_title("20261020T010000Z")),
    ]
    p = plan(groups, allowed={-1, -2, -3, -4, -5, -6})
    assert [c.chat_id for c in p.keep] == [-2, -3]
    assert [c.chat_id for c in p.delete] == [-1]
    assert [c.chat_id for c in p.outside_allowlist] == [-7]
    with pytest.raises(E2EError):
        plan(groups, allowed=set(), keep=0)


def test_plan_keeps_everything_when_at_most_keep_groups() -> None:
    groups = [
        ChatRef(-1, run_title("20261101T010000Z")),
        ChatRef(-2, run_title("20261102T010000Z")),
    ]
    p = plan(groups, allowed={-1, -2})
    assert len(p.keep) == KEEP
    assert p.delete == ()


async def test_dry_run_deletes_nothing() -> None:
    w = World()
    before = set(w.tg.chats)
    result = await w.prune(apply=False)
    assert set(w.tg.chats) == before
    assert w.allowlist.removed == []
    assert "delete_supergroup" not in w.tg.calls
    assert [c.chat_id for c in result.plan.delete] == [w.r2.id, w.r1.id]
    text = result.to_text()
    assert "dry-run" in text
    assert "将删除 2 个：20261102T010000Z-abc123、20261101T010000Z-abc123" in text
    assert "保留最近 2 个：20261114T220000Z-abc123、20261110T010000Z-abc123" in text
    assert "不在白名单中（不删除，仅列出）：1 个：20261020T010000Z-dead00" in text


async def test_apply_deletes_old_groups_and_updates_allowlist() -> None:
    w = World()
    result = await w.prune(apply=True)
    assert result.error is None
    for kept in (w.r3, w.r4, w.resident, w.base_title, w.odd, w.outside, w.foreign):
        assert kept.id in w.tg.chats
    assert w.r1.id not in w.tg.chats
    assert w.r2.id not in w.tg.chats
    assert w.allowlist.removed == [{w.r1.id, w.r2.id}]
    assert w.resident.id in w.allowlist.ids
    text = result.to_text()
    assert "已删除 2 个" in text
    assert "2 个 id 已移除并回读校验" in text
    # Counts and run ids only: no chat id ever appears in the output.
    for chat_id in (w.r1.id, w.r2.id, w.r3.id, w.r4.id, w.outside.id, w.resident.id):
        assert str(chat_id) not in text
        assert str(-chat_id) not in text


async def test_second_apply_is_a_no_op() -> None:
    w = World()
    await w.prune(apply=True)
    result = await w.prune(apply=True)
    assert result.deleted == []
    assert result.error is None
    assert len(w.allowlist.removed) == 1


async def test_failure_midway_still_removes_deleted_ids() -> None:
    w = World()
    # First delete succeeds, second hits a flood wait longer than the pacer allows.
    original = w.admin.delete_supergroup
    calls = 0

    async def flaky(chat_id: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise FloodWait(9999)
        await original(chat_id)

    w.admin.delete_supergroup = flaky  # type: ignore[method-assign]
    result = await w.prune(apply=True)
    assert [c.chat_id for c in result.deleted] == [w.r2.id]
    assert w.r1.id in w.tg.chats
    assert w.allowlist.removed == [{w.r2.id}]
    assert result.error is not None
    assert "超过上限" in result.error
    assert "未删除（中途停止）1 个：20261101T010000Z-abc123" in result.to_text()


async def test_unexpected_transport_error_hides_chat_id() -> None:
    w = World()

    async def broken(chat_id: int) -> None:
        raise RuntimeError(f"CHANNEL_INVALID {chat_id}")

    w.admin.delete_supergroup = broken  # type: ignore[method-assign]
    result = await w.prune(apply=True)
    assert result.deleted == []
    assert result.error == "删除运行 20261102T010000Z-abc123 的验收群失败：RuntimeError"
    assert w.allowlist.removed == []


async def test_allowlist_failure_is_reported() -> None:
    w = World()
    w.allowlist.fail = True
    result = await w.prune(apply=True)
    assert len(result.deleted) == 2
    assert not result.allowlist_updated
    assert result.error is not None
    assert "allowed_chat_ids" in result.error
    assert "移除失败" in result.to_text()


def test_title_matches_the_staging_release_template() -> None:
    match = re.search(r"^E2E_CHAT_TITLE=(.+)$", ENV_TPL.read_text(), re.MULTILINE)
    assert match is not None
    assert match.group(1).strip() == STAGING_RELEASE_CHAT_TITLE


# --- command line ------------------------------------------------------------


class CliHarness:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.world = World()
        self.closed: list[FakeAccount] = []

        async def connect(session: Secret) -> FakeAccount:
            return self.world.admin

        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        monkeypatch.setattr(cli, "LOCK_POLL", 0.01)
        monkeypatch.setattr(cli, "_owner_connect", lambda api: connect)
        monkeypatch.setattr(cli, "VaultAllowlist", lambda vault: self.world.allowlist)
        monkeypatch.setattr(cli, "_vault", lambda: object())
        monkeypatch.setattr(cli, "Pacer", functools.partial(Pacer, sleep=_no_sleep))
        monkeypatch.setattr(cli, "_setup_logging", lambda: None)


def test_cli_defaults_to_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    h = CliHarness(tmp_path, monkeypatch)
    before = set(h.world.tg.chats)
    assert cli.main(["prune-groups"], env=live_env()) == cli.EXIT_PASSED
    assert set(h.world.tg.chats) == before
    assert h.world.admin.closed
    assert "将删除 2 个" in capsys.readouterr().out


def test_cli_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    h = CliHarness(tmp_path, monkeypatch)
    assert cli.main(["prune-groups", "--apply"], env=live_env()) == cli.EXIT_PASSED
    assert h.world.r1.id not in h.world.tg.chats
    assert "已删除 2 个" in capsys.readouterr().out


def test_cli_rejects_dry_run_with_apply() -> None:
    with pytest.raises(SystemExit):
        cli.main(["prune-groups", "--dry-run", "--apply"], env=live_env())


def test_cli_requires_admin_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    CliHarness(tmp_path, monkeypatch)
    env = live_env(E2E_ADMIN_SESSION="")
    assert cli.main(["prune-groups"], env=env) == cli.EXIT_USAGE
    assert "E2E_ADMIN_SESSION" in capsys.readouterr().err


def test_cli_waits_for_the_run_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fcntl
    import os

    h = CliHarness(tmp_path, monkeypatch)
    lock = tmp_path / "state" / "akkiabot-e2e" / "run.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)

    async def scenario() -> None:
        task = asyncio.create_task(
            asyncio.to_thread(cli.main, ["prune-groups", "--apply"], live_env())
        )
        await asyncio.sleep(0.2)
        assert not task.done()
        assert h.world.r1.id in h.world.tg.chats  # nothing deleted while another run holds it
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        assert await asyncio.wait_for(task, 5) == cli.EXIT_PASSED

    asyncio.run(scenario())
    assert h.world.r1.id not in h.world.tg.chats
