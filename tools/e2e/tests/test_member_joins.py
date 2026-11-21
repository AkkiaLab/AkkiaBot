"""The owner adds the member back, flood waits are itemised, skipped is not passed."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from telethon import errors, types

from akkiabot_e2e import cli
from akkiabot_e2e.config import E2EConfig
from akkiabot_e2e.errors import FloodWait, InviteFailed
from akkiabot_e2e.pacing import Pacer
from akkiabot_e2e.redaction import Redactor
from akkiabot_e2e.report import Report
from akkiabot_e2e.runner import run_acceptance
from akkiabot_e2e.scenarios import SCENARIOS, select_scenarios
from akkiabot_e2e.telethon_transport import _check_invited, _invite_error
from akkiabot_e2e.testing import (
    ADMIN,
    BOT,
    MEMBER,
    Connector,
    FakeAccount,
    FakeRegistry,
    FakeTelegram,
    FakeTraces,
    FakeWeb,
    live_env,
)
from akkiabot_e2e.transport import BOT_ADMIN_RIGHTS

NOW = datetime(2026, 11, 15, 8, 0, tzinfo=UTC)


async def _no_sleep(_: float) -> None:
    return None


def _pacer() -> Pacer:
    return Pacer(send_interval=0, sleep=_no_sleep, now=lambda: NOW)


async def _run(
    world: FakeTelegram,
    config: E2EConfig,
    selectors: list[str] | None,
    *,
    pacer: Pacer | None = None,
    catalog: bool = True,
) -> Report:
    return await run_acceptance(
        config,
        select_scenarios(selectors),
        connect=Connector(world),
        run_id="test-run",
        allow_create=True,
        pacer=pacer or _pacer(),
        poll_interval=0.01,
        traces=FakeTraces(),
        registry=FakeRegistry(),
        web=FakeWeb(world),
        catalog=SCENARIOS if catalog else None,
    )


def _group(world: FakeTelegram):
    (chat,) = [c for c in world.chats.values() if c.id < 0]
    return chat


async def test_new_group_and_ban_restore_use_the_owner_not_a_link(
    world: FakeTelegram, config: E2EConfig
) -> None:
    report = await _run(world, config, ["moderation.ban"])

    assert report.status == "passed", report.to_markdown(Redactor())
    assert "join_by_link" not in world.calls
    assert "export_invite_link" not in world.calls
    assert _group(world).members[MEMBER.id] == "member"
    # Once when preparing the new group, once after /ban.
    assert report.joins == {
        "member_join_by_link": 0,
        "member_join_request": 0,
        "owner_added_member": 2,
    }
    assert "群主把普通成员账号拉入群" in report.setup
    # Default privacy: only mutual contacts may add the member.
    assert "两个测试账号互加联系人（群主拉人入群需要双向联系人）" in report.setup
    assert world.contacts[MEMBER.id] == {ADMIN.id}
    assert world.contacts[ADMIN.id] == {MEMBER.id}
    md = report.to_markdown(Redactor())
    assert "本轮入群：成员通过链接入群 0 次，群主拉人 2 次" in md
    assert "FloodWait：本轮 0 次" in md


async def test_full_run_joins_by_link_only_for_captcha_and_service_message(
    world: FakeTelegram, config: E2EConfig
) -> None:
    report = await _run(world, config, None)

    assert report.status == "passed", report.to_markdown(Redactor())
    assert report.joins["member_join_by_link"] == 2  # captcha.verify, members.service-message
    assert report.joins["member_join_request"] == 1  # members.join-request (declined, no join)
    # New group, then moderation.warn, moderation.ban and antispam.spam-command each ban and restore;
    # members.join-request declines the request and adds the member back.
    assert report.joins["owner_added_member"] == 5
    assert report.summary == {"passed": len(SCENARIOS), "failed": 0, "skipped": 0}
    assert report.fully_executed()


@pytest.mark.parametrize("ignored", [False, True])
async def test_failed_owner_add_stops_the_run_without_link_fallback(
    world: FakeTelegram, config: E2EConfig, ignored: bool
) -> None:
    chat = world.add_chat(config.chat_title, ADMIN)  # fixed group, member inside
    chat.members.update({BOT.id: "admin", MEMBER.id: "member"})
    chat.rights[BOT.id] = BOT_ADMIN_RIGHTS
    config = replace(config, allowed_chat_ids=frozenset({chat.id}))
    if ignored:
        world.invite_ignored.add(MEMBER.id)
    else:
        world.invite_errors[MEMBER.id] = "USER_PRIVACY_RESTRICTED"

    report = await _run(world, config, ["moderation.ban", "members.service-message"])

    ban, later = report.results[0], report.results[1]
    assert (ban.name, ban.status) == ("moderation.ban", "failed")
    assert "恢复成员失败" in ban.detail
    if not ignored:
        assert "USER_PRIVACY_RESTRICTED" in ban.detail
    # Not run at all: it would have joined by link, hiding the failed restore.
    assert (later.name, later.status, later.skip_reason) == (
        "members.service-message",
        "skipped",
        "aborted",
    )
    assert "join_by_link" not in world.calls
    assert report.status == "failed"


async def test_prepare_stops_when_owner_cannot_add_the_member(
    world: FakeTelegram, config: E2EConfig
) -> None:
    world.invite_errors[MEMBER.id] = "PEER_FLOOD"

    report = await _run(world, config, ["commands.help"])

    assert report.status == "failed"
    assert "准备阶段失败" in report.detail
    assert "PEER_FLOOD" in report.detail
    assert "join_by_link" not in world.calls
    # Every scenario is still listed: the selected one as aborted, the rest unselected.
    by_name = {r.name: r for r in report.results}
    assert set(by_name) == {s.name for s in SCENARIOS}
    assert (by_name["commands.help"].status, by_name["commands.help"].skip_reason) == (
        "skipped",
        "aborted",
    )
    assert {r.skip_reason for n, r in by_name.items() if n != "commands.help"} == {"not_selected"}
    assert report.summary == {"passed": 0, "failed": 0, "skipped": len(SCENARIOS)}
    assert report.joins == {
        "member_join_by_link": 0,
        "member_join_request": 0,
        "owner_added_member": 0,
    }
    data = json.loads(report.to_json(Redactor()))
    assert data["summary"] == report.summary
    assert data["results"][0]["skip_reason"] == "aborted"
    md = report.to_markdown(Redactor())
    assert f"实际通过 0 / 失败 0 / 跳过 {len(SCENARIOS)}（不计为通过）" in md
    assert (
        "| `commands.help` /help 返回命令列表 | commands | ⏭️ skipped | 运行中止：准备阶段失败" in md
    )
    assert "成员通过链接入群 0 次，群主拉人 0 次" in md


async def test_prepare_rate_limit_lists_scenarios_and_keeps_join_counts(
    world: FakeTelegram, config: E2EConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The owner adds the member, then the membership check hits a long flood wait.
    original = FakeAccount.participant_role

    async def participant_role(self: FakeAccount, chat_id: int, user_id: int):
        if user_id == MEMBER.id and "invite" in self.world.calls:
            members = self.world.chats[chat_id].members
            if MEMBER.id in members:
                raise FloodWait(3600)
        return await original(self, chat_id, user_id)

    monkeypatch.setattr(FakeAccount, "participant_role", participant_role)

    report = await _run(world, config, ["moderation.ban", "members.service-message"])

    assert report.status == "failed"
    assert "准备阶段失败" in report.detail
    by_name = {r.name: r for r in report.results}
    assert [r.name for r in report.results[:2]] == ["moderation.ban", "members.service-message"]
    for name in ("moderation.ban", "members.service-message"):
        assert (by_name[name].status, by_name[name].skip_reason) == ("skipped", "rate_limited")
    assert by_name["commands.help"].skip_reason == "not_selected"
    # The add completed before the run stopped: it is counted.
    assert report.joins == {
        "member_join_by_link": 0,
        "member_join_request": 0,
        "owner_added_member": 1,
    }
    (event,) = report.flood_waits
    assert (event["method"], event["seconds"], event["aborted"]) == ("participant_role", 3600, True)
    data = json.loads(report.to_json(Redactor()))
    assert data["joins"]["owner_added_member"] == 1
    assert data["summary"] == {"passed": 0, "failed": 0, "skipped": len(SCENARIOS)}
    md = report.to_markdown(Redactor())
    assert (
        "| `moderation.ban` /ban 封禁成员并删除其历史消息 | moderation | ⏭️ skipped | 限流中止：准备阶段因限流中止"
        in md
    )
    assert "成员通过链接入群 0 次，群主拉人 1 次" in md
    assert "超过上限，中止" in md


async def test_flood_waits_are_itemised(world: FakeTelegram, config: E2EConfig) -> None:
    world.floods["invite"] = [5, 7]  # the owner adding the bot, then the member

    report = await _run(world, config, ["commands.help"])

    assert report.status == "passed", report.to_markdown(Redactor())
    assert report.flood_waits == [
        {
            "at": "2026-11-15T08:00:00+00:00",
            "account": "admin",
            "method": "invite",
            "seconds": 5,
            "attempt": 1,
            "aborted": False,
        },
        {
            "at": "2026-11-15T08:00:00+00:00",
            "account": "admin",
            "method": "invite",
            "seconds": 7,
            "attempt": 2,
            "aborted": False,
        },
    ]
    assert report.flood_wait_seconds == 14
    md = report.to_markdown(Redactor())
    assert "| 2026-11-15T08:00:00+00:00 | admin | `invite` | 7 | 2 | 等待后重试 |" in md


async def test_aborting_flood_wait_is_recorded(world: FakeTelegram, config: E2EConfig) -> None:
    world.floods["send"] = [3600]
    pacer = Pacer(max_wait=60, send_interval=0, sleep=_no_sleep, now=lambda: NOW)

    report = await _run(world, config, ["commands.help", "commands.id"], pacer=pacer)

    (event,) = report.flood_waits
    assert (event["method"], event["seconds"], event["attempt"], event["aborted"]) == (
        "send",
        3600,
        1,
        True,
    )
    assert event["account"] in {"admin", "member"}
    assert report.results[1].skip_reason == "rate_limited"
    assert "超过上限，中止" in report.to_markdown(Redactor())


async def test_unselected_scenarios_are_skipped_not_passed(
    world: FakeTelegram, config: E2EConfig
) -> None:
    report = await _run(world, config, ["commands.help"])

    assert report.status == "passed"
    assert report.results[0].name == "commands.help"
    skipped = [r for r in report.results if r.status == "skipped"]
    assert len(skipped) == len(SCENARIOS) - 1
    assert {r.skip_reason for r in skipped} == {"not_selected"}
    assert report.summary == {"passed": 1, "failed": 0, "skipped": len(SCENARIOS) - 1}
    assert not report.fully_executed()
    md = report.to_markdown(Redactor())
    assert f"实际通过 1 / 失败 0 / 跳过 {len(SCENARIOS) - 1}（不计为通过）" in md
    assert "未选中（本次改动未涉及）" in md


async def test_missing_credentials_skip_fails_the_run(
    world: FakeTelegram, config: E2EConfig
) -> None:
    from akkiabot_e2e.config import load_config

    config = load_config(live_env(LLM_API_KEY="", LLM_MODEL=""))
    report = await _run(world, config, ["antispam.enabled", "commands.help"])

    (spam,) = [r for r in report.results if r.name == "antispam.enabled"]
    assert (spam.status, spam.skip_reason) == ("skipped", "missing_credentials")
    assert report.status == "failed"
    assert "缺少凭据或配置" in report.to_markdown(Redactor())


def test_cli_rejects_fresh_group_for_pr_level_runs(tmp_path: Path) -> None:
    code = cli.main(
        ["run", "--serve", "--fresh-group", "--report-dir", str(tmp_path)], env=live_env()
    )
    assert code == cli.EXIT_USAGE


def test_telethon_invite_error_names_the_rpc_code() -> None:
    exc = errors.UserPrivacyRestrictedError(request=None)
    assert "USER_PRIVACY_RESTRICTED" in _invite_error(42, exc)


def test_telethon_missing_invitees_raise() -> None:
    result = types.messages.InvitedUsers(
        updates=types.Updates(updates=[], users=[], chats=[], date=NOW, seq=0),
        missing_invitees=[types.MissingInvitee(user_id=42)],
    )
    with pytest.raises(InviteFailed, match="missing_invitees"):
        _check_invited(42, result)
    _check_invited(7, result)  # someone else missing: not our concern
    _check_invited(42, object())  # older layers return plain updates


async def test_pacer_counts_invites_per_user() -> None:
    pacer = _pacer()

    async def ok() -> None:
        return None

    await pacer.call("admin.invite", ok, count_as="admin.invite:1")
    await pacer.call("member.join_by_link", ok)
    assert pacer.completed == {"admin.invite:1": 1, "member.join_by_link": 1}


async def test_contact_is_added_once(world: FakeTelegram, config: E2EConfig) -> None:
    chat = world.add_chat(config.chat_title, ADMIN)
    chat.members.update({BOT.id: "admin"})
    chat.rights[BOT.id] = BOT_ADMIN_RIGHTS
    config = replace(config, allowed_chat_ids=frozenset({chat.id}))
    world.contacts[MEMBER.id].add(ADMIN.id)  # already mutual contacts
    world.contacts[ADMIN.id].add(MEMBER.id)

    report = await _run(world, config, ["commands.help"])

    assert report.status == "passed", report.to_markdown(Redactor())
    assert report.setup == ["复用测试群", "群主把普通成员账号拉入群"]


async def test_one_sided_contact_is_completed(world: FakeTelegram, config: E2EConfig) -> None:
    world.contacts[MEMBER.id].add(ADMIN.id)  # only the member added the owner

    report = await _run(world, config, ["moderation.ban"])

    assert report.status == "passed", report.to_markdown(Redactor())
    assert world.contacts[ADMIN.id] == {MEMBER.id}
