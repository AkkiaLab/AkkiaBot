"""Fault injection for create-group: interrupted / partially failed runs never yield a 2nd group."""

from __future__ import annotations

import pytest

from akkiabot_e2e.config import E2EConfig, load_config
from akkiabot_e2e.errors import ConfigError, SafetyError
from akkiabot_e2e.group import operation_marker, select_group
from akkiabot_e2e.pacing import Pacer
from akkiabot_e2e.redaction import Redactor
from akkiabot_e2e.report import Report
from akkiabot_e2e.runner import run_acceptance
from akkiabot_e2e.scenarios import select_scenarios
from akkiabot_e2e.testing import (
    ADMIN,
    BOT,
    MEMBER,
    Connector,
    FakeRegistry,
    FakeTelegram,
    Interrupted,
    live_env,
)
from akkiabot_e2e.transport import BOT_ADMIN_RIGHTS

TITLE = "AkkiaBot E2E 自动验收群"


async def _no_sleep(_: float) -> None:
    return None


async def run(
    world: FakeTelegram,
    registry: FakeRegistry,
    *,
    allow_create: bool = True,
    config: E2EConfig | None = None,
) -> Report:
    """One acceptance run whose config reflects the registry's allowlist (like ``op run``)."""
    if config is None:
        ids = ",".join(map(str, sorted(registry.allowed)))
        config = load_config(live_env(E2E_ALLOWED_CHAT_IDS=ids, E2E_REPLY_TIMEOUT="0.3"))
    return await run_acceptance(
        config,
        select_scenarios(["m2.help"]),
        connect=Connector(world),
        run_id="test-run",
        allow_create=allow_create,
        pacer=Pacer(send_interval=0, sleep=_no_sleep),
        poll_interval=0.01,
        registry=registry,
    )


def groups(world: FakeTelegram) -> list[int]:
    return [c.id for c in world.chats.values() if c.id < 0]


def creates(world: FakeTelegram) -> int:
    return world.calls.count("create_supergroup")


async def test_normal_create_records_operation_then_registers_and_clears(
    world: FakeTelegram,
) -> None:
    registry = FakeRegistry()

    report = await run(world, registry)

    assert report.status == "passed", report.to_markdown(Redactor())
    (chat_id,) = groups(world)
    assert registry.allowed == {chat_id}
    assert registry.pending is None
    assert registry.calls == ["begin", "register", "finish"]
    assert "[akkiabot-e2e op:" in world.chats[chat_id].about
    assert report.chat == {"id": chat_id, "title": TITLE, "created": True}
    assert any("已写入 1Password allowed_chat_ids" in step for step in report.setup)


async def test_normal_reuse_does_not_touch_the_registry(world: FakeTelegram) -> None:
    chat = world.add_chat(TITLE, ADMIN)
    chat.members.update({BOT.id: "admin", MEMBER.id: "member"})
    chat.rights[BOT.id] = BOT_ADMIN_RIGHTS
    registry = FakeRegistry(allowed={chat.id})
    world.contacts[MEMBER.id].add(ADMIN.id)  # set up by an earlier run
    world.contacts[ADMIN.id].add(MEMBER.id)

    report = await run(world, registry)

    assert report.status == "passed", report.to_markdown(Redactor())
    assert report.setup == ["复用测试群"]
    assert registry.calls == []
    assert creates(world) == 0


async def test_interrupted_after_create_is_recovered_by_rerun(world: FakeTelegram) -> None:
    registry = FakeRegistry()
    registry.fail["register"] = Interrupted()

    with pytest.raises(Interrupted):  # the process "dies" right after creating the group
        await run(world, registry)

    (chat_id,) = groups(world)
    assert registry.allowed == set()
    op_id = registry.pending
    assert op_id is not None

    report = await run(world, registry)

    assert report.status == "passed", report.to_markdown(Redactor())
    assert groups(world) == [chat_id]
    assert creates(world) == 1
    assert registry.allowed == {chat_id}
    assert registry.pending is None
    assert report.chat == {"id": chat_id, "title": TITLE, "created": False}
    assert any(f"按建群操作 {op_id} 找回" in step for step in report.setup)

    # Afterwards it is just the normal allowlisted group.
    report = await run(world, registry)
    assert report.status == "passed"
    assert report.setup[0] == "复用测试群"
    assert creates(world) == 1


async def test_rerun_without_create_group_explains_recovery(world: FakeTelegram) -> None:
    registry = FakeRegistry()
    registry.fail["register"] = Interrupted()
    with pytest.raises(Interrupted):
        await run(world, registry)

    report = await run(world, registry, allow_create=False)

    assert report.status == "failed"
    assert "加 --create-group 重新运行即可按标识找回" in (report.detail or "")
    assert registry.allowed == set()
    assert creates(world) == 1


async def test_allowlist_write_failure_fails_clearly_and_rerun_recovers(
    world: FakeTelegram,
) -> None:
    registry = FakeRegistry()
    registry.fail["register"] = ConfigError("写入 1Password 条目失败：network down")

    report = await run(world, registry)

    (chat_id,) = groups(world)
    assert report.status == "failed"
    detail = report.detail or ""
    assert f"测试群 {chat_id} 已创建，但写入 allowed_chat_ids 失败" in detail
    assert "不要删除该群" in detail
    assert f"建群操作 {registry.pending}" in detail
    assert report.results == []  # no scenario ran

    report = await run(world, registry)

    assert report.status == "passed", report.to_markdown(Redactor())
    assert groups(world) == [chat_id]
    assert registry.allowed == {chat_id}


async def test_recording_the_operation_fails_before_any_group_exists(
    world: FakeTelegram,
) -> None:
    registry = FakeRegistry()
    registry.fail["begin"] = ConfigError("写入 1Password 条目失败：network down")

    report = await run(world, registry)

    assert report.status == "failed"
    assert "建群前记录建群操作失败，未创建任何群" in (report.detail or "")
    assert groups(world) == []
    assert creates(world) == 0


async def test_interrupted_before_create_reuses_the_same_operation(world: FakeTelegram) -> None:
    # Pending operation recorded, but the process died before the group was created.
    registry = FakeRegistry(pending="0123abcd4567")

    report = await run(world, registry)

    assert report.status == "passed", report.to_markdown(Redactor())
    (chat_id,) = groups(world)
    assert operation_marker("0123abcd4567") in world.chats[chat_id].about
    assert registry.allowed == {chat_id}
    assert registry.pending is None


async def test_failing_to_clear_the_operation_is_not_fatal(world: FakeTelegram) -> None:
    registry = FakeRegistry()
    registry.fail["finish"] = ConfigError("写入 1Password 条目失败：network down")

    report = await run(world, registry)

    assert report.status == "passed", report.to_markdown(Redactor())
    (chat_id,) = groups(world)
    assert registry.allowed == {chat_id}
    assert any("清除 pending_group_operation 失败" in step for step in report.setup)
    # Next run reuses the allowlisted group; the stale pending id is harmless.
    report = await run(world, registry)
    assert report.status == "passed"
    assert creates(world) == 1


async def test_multiple_candidates_with_the_marker_fail_without_creating(
    world: FakeTelegram, config: E2EConfig
) -> None:
    registry = FakeRegistry(pending="0123abcd4567")
    for _ in range(2):
        world.add_chat(TITLE, ADMIN).about = operation_marker("0123abcd4567")
    admin = world.account(ADMIN)

    with pytest.raises(SafetyError, match="多个同名测试群带有建群操作 0123abcd4567 的标识"):
        await select_group(admin, config, allow_create=True, registry=registry)

    assert creates(world) == 0
    assert registry.allowed == set()


async def test_same_title_group_without_marker_is_never_claimed(
    world: FakeTelegram, config: E2EConfig
) -> None:
    world.add_chat(TITLE, ADMIN).about = "someone else's group"
    world.add_chat(TITLE, ADMIN).about = operation_marker("ffffeeeedddd")  # another operation
    admin = world.account(ADMIN)

    registry = FakeRegistry(pending="0123abcd4567")
    with pytest.raises(SafetyError, match="都没有未完成建群操作 0123abcd4567 的标识"):
        await select_group(admin, config, allow_create=True, registry=registry)

    registry = FakeRegistry()  # no pending operation at all
    with pytest.raises(SafetyError, match="没有未完成的建群记录"):
        await select_group(admin, config, allow_create=True, registry=registry)

    assert creates(world) == 0
    assert registry.allowed == set()


async def test_create_group_requires_a_registry(world: FakeTelegram, config: E2EConfig) -> None:
    with pytest.raises(SafetyError, match="白名单存储"):
        await select_group(world.account(ADMIN), config, allow_create=True)
    assert creates(world) == 0
