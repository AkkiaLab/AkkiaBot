"""Find (or, when explicitly allowed, create) the dedicated test supergroup.

Safety rules:

* only supergroups **created by the admin test account** with the exact
  configured title are considered;
* an existing group is used only if its id is in ``E2E_ALLOWED_CHAT_IDS``;
* a group is created only with ``--create-group``; it is never deleted, and
  its history is never cleared by this tool.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from akkiabot_e2e.config import CHAT_ABOUT, E2EConfig
from akkiabot_e2e.errors import SafetyError
from akkiabot_e2e.transport import BOT_ADMIN_RIGHTS, Account, ChatRef, UserInfo

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TestGroup:
    chat: ChatRef
    created: bool
    actions: tuple[str, ...]
    """Human-readable setup steps performed in this run (for the report)."""


async def select_group(admin: Account, config: E2EConfig, *, allow_create: bool) -> TestGroup:
    owned = await admin.owned_supergroups(config.chat_title)
    allowed = [chat for chat in owned if chat.id in config.allowed_chat_ids]
    if len(allowed) > 1:
        ids = ", ".join(str(c.id) for c in allowed)
        raise SafetyError(f"白名单中有多个同名测试群（{ids}），请只保留一个")
    if allowed:
        return TestGroup(allowed[0], created=False, actions=("复用测试群",))
    if owned and not allow_create:
        ids = ", ".join(str(c.id) for c in owned)
        raise SafetyError(
            f"找到同名测试群 {ids}，但不在 E2E_ALLOWED_CHAT_IDS 中。"
            "确认是本工具创建的测试群后，把 id 加入 1Password 的 allowed_chat_ids 再运行"
        )
    if not allow_create:
        raise SafetyError(
            "没有找到可用的测试群。首次运行请加 --create-group 创建，"
            "随后把报告中的群 id 写入 1Password 的 allowed_chat_ids"
        )
    chat = await admin.create_supergroup(config.chat_title, CHAT_ABOUT)
    logger.info("created test group %s", chat.id)
    return TestGroup(chat, created=True, actions=(f"创建测试群 {chat.id}",))


async def prepare_group(
    admin: Account,
    member: Account,
    *,
    admin_user: UserInfo,
    member_user: UserInfo,
    bot: UserInfo,
    config: E2EConfig,
    allow_create: bool,
) -> TestGroup:
    """Select the group and make sure the bot (admin, with rights) and the member are in it."""
    group = await select_group(admin, config, allow_create=allow_create)
    chat_id = group.chat.id
    actions = list(group.actions)

    admin_role = await admin.participant_role(chat_id, admin_user.id)
    if admin_role not in {"creator", "admin"}:
        raise SafetyError("群主测试账号不是测试群的群主或管理员")

    # Bot first: it must already be an admin when the member's join message arrives.
    if await admin.participant_role(chat_id, bot.id) is None:
        await admin.invite(chat_id, bot.id)
        actions.append("邀请测试 bot 入群")
    current = await admin.admin_rights(chat_id, bot.id)
    if BOT_ADMIN_RIGHTS - current:
        # Keep rights granted by hand; only add what is missing.
        await admin.promote(chat_id, bot.id, current | BOT_ADMIN_RIGHTS)
        actions.append(f"授予 bot 管理员权限：{', '.join(sorted(BOT_ADMIN_RIGHTS))}")

    if await admin.is_banned(chat_id, member_user.id):
        # Left over by an interrupted M4 run (/warn, /ban ban the member, then restore it).
        await admin.unban(chat_id, member_user.id)
        actions.append("解除普通成员账号的封禁（上次运行中断时遗留）")
    elif await admin.is_restricted(chat_id, member_user.id):
        # Left over by an interrupted M5 run (spam detection mutes the member).
        await admin.unban(chat_id, member_user.id)
        actions.append("解除普通成员账号的禁言（上次运行中断时遗留）")
    if await member.find_chat(chat_id) is None:
        link = await admin.export_invite_link(chat_id)
        await member.join_by_link(link)
        actions.append("普通成员账号通过邀请链接入群")
    if await admin.participant_role(chat_id, member_user.id) != "member":
        raise SafetyError("普通成员测试账号在测试群中不是普通成员（不应是管理员）")

    return TestGroup(group.chat, created=group.created, actions=tuple(actions))
