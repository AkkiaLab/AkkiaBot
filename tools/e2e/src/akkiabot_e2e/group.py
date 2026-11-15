"""Find (or, when explicitly allowed, create) the dedicated test supergroup.

Safety rules:

* only supergroups **created by the admin test account** with the exact
  configured title are considered;
* an existing group is used only if its id is in ``E2E_ALLOWED_CHAT_IDS``;
* a group is created only with ``--create-group``, and never while a same-title
  group outside the allowlist exists (that may be one we created and failed to
  register); it is never deleted, and its history is never cleared by this tool.

Creating a group is a multi-step operation that can be interrupted half way, so
it is made recoverable (see docs/e2e-acceptance.md, "建群的中断恢复"):

1. a fresh operation id is recorded in the :class:`GroupRegistry` (1Password)
   *before* the group is created — if that fails, nothing is created;
2. the group is created with the operation id embedded in its description;
3. the new id is added to the allowlist right away, then the pending operation
   is cleared.

When a later ``--create-group`` run finds same-title groups outside the
allowlist, it adopts one only if exactly one of them carries the pending
operation id in its description; otherwise it stops and explains how to
recover by hand. A same title alone never identifies a group.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Protocol

from akkiabot_e2e.config import CHAT_ABOUT, E2EConfig
from akkiabot_e2e.errors import E2EError, InviteFailed, SafetyError
from akkiabot_e2e.transport import BOT_ADMIN_RIGHTS, Account, ChatRef, UserInfo
from akkiabot_e2e.vault import E2E_ITEM, FieldValue, Vault

logger = logging.getLogger(__name__)

PENDING_FIELD = "pending_group_operation"
ALLOWED_CHATS_FIELD = "allowed_chat_ids"


def operation_marker(op_id: str) -> str:
    """Tag written into the group description; identifies the group of one operation."""
    return f"[akkiabot-e2e op:{op_id}]"


def new_operation_id() -> str:
    return uuid.uuid4().hex[:12]


def fresh_group_title(base: str, run_id: str) -> str:
    """Title of the group a ``--fresh-group`` run creates for itself.

    A long-running bot keeps per-chat state (member message counts, switches,
    warnings) across runs, so reusing one group makes later runs differ from the
    first: the member soon has more than 3 recorded messages and is exempt from
    the spam check. A group of its own gives every run the state of a first run.
    The run id makes the title unique, so the group is never matched by another run.
    """
    return f"{base} {run_id}"


class GroupRegistry(Protocol):
    """Durable store for the chat allowlist and the pending create-group operation."""

    def pending_operation(self) -> str | None: ...

    def begin(self, op_id: str) -> None:
        """Record ``op_id`` as pending; must be durable before the group is created."""
        ...

    def register(self, chat_id: int) -> None:
        """Add ``chat_id`` to the chat allowlist."""
        ...

    def finish(self) -> None:
        """Clear the pending operation."""
        ...


class VaultGroupRegistry:
    """:class:`GroupRegistry` backed by the ``e2e-telegram`` 1Password item."""

    def __init__(self, vault: Vault) -> None:
        self.vault = vault

    def pending_operation(self) -> str | None:
        return (self.vault.read(E2E_ITEM, PENDING_FIELD) or "").strip() or None

    def begin(self, op_id: str) -> None:
        self.vault.set_fields(E2E_ITEM, {PENDING_FIELD: FieldValue(op_id, concealed=False)})

    def register(self, chat_id: int) -> None:
        self.vault.add_ids(E2E_ITEM, ALLOWED_CHATS_FIELD, [chat_id])

    def finish(self) -> None:
        self.vault.clear_field(E2E_ITEM, PENDING_FIELD)


@dataclass(frozen=True, slots=True)
class TestGroup:
    chat: ChatRef
    created: bool
    actions: tuple[str, ...]
    """Human-readable setup steps performed in this run (for the report)."""
    recovered: bool = False
    """Adopted from an interrupted create-group operation."""


_MANUAL_RECOVERY = (
    "不会自动认领，也不会再建新群。请在群主测试账号的客户端中确认哪个是本工具创建的测试群，"
    "把它的 id 写入 1Password 的 e2e-telegram/allowed_chat_ids，"
    "再把 e2e-telegram/pending_group_operation 清空后重新运行（不要删除群）"
)


async def _finish(registry: GroupRegistry, actions: list[str]) -> None:
    try:
        await asyncio.to_thread(registry.finish)
    except E2EError as exc:
        # Harmless: a stale pending id only matters for a group carrying its marker.
        logger.warning("could not clear the pending create-group operation: %s", exc)
        actions.append(f"清除 pending_group_operation 失败（不影响本次运行，可手动清空）：{exc}")


async def _recover(
    admin: Account, registry: GroupRegistry, candidates: list[ChatRef], pending: str
) -> TestGroup:
    marker = operation_marker(pending)
    matches = [c for c in candidates if marker in await admin.chat_about(c.id)]
    ids = ", ".join(str(c.id) for c in candidates)
    if not matches:
        raise SafetyError(
            f"找到不在白名单中的同名测试群 {ids}，但都没有未完成建群操作 {pending} 的标识。"
            + _MANUAL_RECOVERY
        )
    if len(matches) > 1:
        raise SafetyError(
            f"有多个同名测试群带有建群操作 {pending} 的标识"
            f"（{', '.join(str(c.id) for c in matches)}），无法唯一确认。" + _MANUAL_RECOVERY
        )
    (chat,) = matches
    await asyncio.to_thread(registry.register, chat.id)
    actions = [
        f"按建群操作 {pending} 找回上次中断时创建的测试群 {chat.id}，已写入 allowed_chat_ids"
    ]
    await _finish(registry, actions)
    logger.info("recovered test group %s from operation %s", chat.id, pending)
    return TestGroup(chat, created=False, actions=tuple(actions), recovered=True)


async def select_group(
    admin: Account,
    config: E2EConfig,
    *,
    allow_create: bool,
    registry: GroupRegistry | None = None,
) -> TestGroup:
    owned = await admin.owned_supergroups(config.chat_title)
    allowed = [chat for chat in owned if chat.id in config.allowed_chat_ids]
    if len(allowed) > 1:
        ids = ", ".join(str(c.id) for c in allowed)
        raise SafetyError(f"白名单中有多个同名测试群（{ids}），请只保留一个")
    if allowed:
        return TestGroup(allowed[0], created=False, actions=("复用测试群",))

    if allow_create and registry is None:
        raise SafetyError("建群需要能写入 allowed_chat_ids 的白名单存储（1Password）")
    pending = await asyncio.to_thread(registry.pending_operation) if registry else None
    if owned:
        ids = ", ".join(str(c.id) for c in owned)
        if not allow_create:
            hint = (
                f"1Password 中记录了未完成的建群操作 {pending}，加 --create-group 重新运行即可按标识找回该群"
                if pending
                else "确认是本工具创建的测试群后，把 id 加入 1Password 的 allowed_chat_ids 再运行"
            )
            raise SafetyError(f"找到同名测试群 {ids}，但不在 E2E_ALLOWED_CHAT_IDS 中。{hint}")
        assert registry is not None  # noqa: S101 - checked above
        if pending is None:
            raise SafetyError(
                f"找到不在白名单中的同名测试群 {ids}，且没有未完成的建群记录。" + _MANUAL_RECOVERY
            )
        return await _recover(admin, registry, owned, pending)
    if not allow_create:
        raise SafetyError(
            "没有找到可用的测试群。首次运行请加 --create-group 创建，"
            "工具会把新群 id 写入 1Password 的 allowed_chat_ids"
        )
    assert registry is not None  # noqa: S101 - checked above

    # Reuse a pending id whose group was never created (interrupted before step 2).
    op_id = pending or new_operation_id()
    try:
        await asyncio.to_thread(registry.begin, op_id)
    except E2EError as exc:
        raise SafetyError(f"建群前记录建群操作失败，未创建任何群：{exc}") from exc
    chat = await admin.create_supergroup(
        config.chat_title, f"{CHAT_ABOUT}\n{operation_marker(op_id)}"
    )
    logger.info("created test group %s (operation %s)", chat.id, op_id)
    try:
        await asyncio.to_thread(registry.register, chat.id)
    except E2EError as exc:
        raise SafetyError(
            f"测试群 {chat.id} 已创建，但写入 allowed_chat_ids 失败：{exc}。"
            f"不要删除该群；排除 1Password 故障后执行 just e2e --create-group，"
            f"工具会按建群操作 {op_id} 找回它，不会再建新群"
        ) from exc
    actions = [
        f"创建测试群 {chat.id}（建群操作 {op_id}）",
        "新群 id 已写入 1Password allowed_chat_ids",
    ]
    await _finish(registry, actions)
    return TestGroup(chat, created=True, actions=tuple(actions))


async def owner_add_member(admin: Account, chat_id: int, user_id: int) -> None:
    """The owner account adds ``user_id`` to the group (``channels.inviteToChannel``).

    Used instead of the member joining through an invite link everywhere except
    the scenarios that verify a real join. Failures (``USER_PRIVACY_RESTRICTED``,
    ``PEER_FLOOD`` …) raise :class:`InviteFailed`; there is deliberately no
    fallback to an invite link.
    """
    await admin.invite(chat_id, user_id)
    if await admin.participant_role(chat_id, user_id) is None:
        raise InviteFailed(f"群主账号拉入用户 {user_id} 后，该用户仍不在测试群中")


async def prepare_group(
    admin: Account,
    member: Account,
    *,
    admin_user: UserInfo,
    member_user: UserInfo,
    bot: UserInfo,
    config: E2EConfig,
    allow_create: bool,
    registry: GroupRegistry | None = None,
) -> TestGroup:
    """Select the group and make sure the bot (admin, with rights) and the member are in it."""
    group = await select_group(admin, config, allow_create=allow_create, registry=registry)
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
        # The owner adds the member: joins by link are what Telegram rate-limits
        # per account, and only m7.captcha / m2.service-message need a real one.
        await owner_add_member(admin, chat_id, member_user.id)
        actions.append("群主把普通成员账号拉入群")
    if await admin.participant_role(chat_id, member_user.id) != "member":
        raise SafetyError("普通成员测试账号在测试群中不是普通成员（不应是管理员）")

    return TestGroup(
        group.chat, created=group.created, actions=tuple(actions), recovered=group.recovered
    )
